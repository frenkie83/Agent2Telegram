"""Crash-safe disk queues used by the v2 Telegram bridge.

Each queue record lives in its own JSON file.  Publishing or updating a record is an
atomic replace of a uniquely named temporary file in the same directory.  Callers must
still hold the process-wide single-instance lock: the classes protect threads and file
integrity, but deliberately do not implement an inter-process ownership policy.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
DEFAULT_MAX_RECORDS = 10_000
DEFAULT_MAX_BYTES = 256 * 1024 * 1024
DEFAULT_PENDING_RETENTION_SECONDS = 30 * 24 * 60 * 60
DEFAULT_DEAD_LETTER_RETENTION_SECONDS = 90 * 24 * 60 * 60
STALE_TEMP_SECONDS = 24 * 60 * 60
MAX_REASON_CHARS = 4_096


class DurableError(RuntimeError):
    """Base class for durable queue failures."""


class CapacityError(DurableError):
    """The configured record-count or byte limit would be exceeded."""


class CorruptRecordError(DurableError):
    """A persisted record cannot be decoded safely."""


class RecordConflictError(DurableError):
    """An idempotency identity already exists with different content."""


class InvalidTransitionError(DurableError):
    """A caller attempted to skip an unconfirmed delivery step."""


@dataclass(frozen=True)
class Record:
    """Immutable view of one inbox or outbox record."""

    id: str
    kind: str
    created_ns: int
    updated_ns: int
    status: str
    attempts: int = 0
    last_error: str | None = None
    update: dict[str, Any] | None = None
    chunks: tuple[str, ...] = ()
    files: tuple[str, ...] = ()
    key: str | None = None
    sent_chunks: frozenset[int] = frozenset()
    sent_files: frozenset[str] = frozenset()

    @property
    def record_id(self) -> str:
        return self.id

    @property
    def pending_chunks(self) -> tuple[tuple[int, str], ...]:
        return tuple(
            (index, chunk)
            for index, chunk in enumerate(self.chunks)
            if index not in self.sent_chunks
        )

    @property
    def pending_files(self) -> tuple[str, ...]:
        return tuple(path for path in self.files if path not in self.sent_files)

    @property
    def complete(self) -> bool:
        return not self.pending_chunks and not self.pending_files


def _json_bytes(data: dict[str, Any]) -> bytes:
    try:
        text = json.dumps(
            data,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise DurableError(f"record is not JSON serializable: {exc}") from exc
    return (text + "\n").encode("utf-8")


def _fsync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    """Write and publish one file durably; every failure propagates to the caller."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.parent / f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    fd = -1
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        # Both names are constructed in path.parent, so replace never intentionally
        # crosses a filesystem boundary.
        os.replace(temp, path)
        _fsync_directory(path.parent)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        try:
            temp.unlink()
        except FileNotFoundError:
            pass
        raise


def _atomic_write_json(path: Path, data: dict[str, Any]) -> int:
    payload = _json_bytes(data)
    _atomic_write_bytes(path, payload)
    return len(payload)


def _safe_reason(reason: str) -> str:
    return str(reason)[:MAX_REASON_CHARS]


def _record_from_data(data: dict[str, Any], expected_kind: str) -> Record:
    try:
        version = int(data["version"])
        kind = str(data["kind"])
        record_id = str(data["id"])
        created_ns = int(data["created_ns"])
        updated_ns = int(data["updated_ns"])
        status = str(data["status"])
        attempts = int(data.get("attempts", 0))
    except (KeyError, TypeError, ValueError) as exc:
        raise CorruptRecordError(f"record metadata is invalid: {exc}") from exc
    if version != SCHEMA_VERSION:
        raise CorruptRecordError(f"unsupported record version {version}")
    if kind != expected_kind:
        raise CorruptRecordError(f"expected {expected_kind!r} record, found {kind!r}")
    if status not in {"pending", "dead"}:
        raise CorruptRecordError(f"invalid record status {status!r}")
    if attempts < 0:
        raise CorruptRecordError("negative attempt count")

    last_error = data.get("last_error")
    if last_error is not None and not isinstance(last_error, str):
        raise CorruptRecordError("last_error is not a string")

    if kind == "inbox":
        update = data.get("update")
        if not isinstance(update, dict):
            raise CorruptRecordError("inbox update is not an object")
        return Record(
            id=record_id,
            kind=kind,
            created_ns=created_ns,
            updated_ns=updated_ns,
            status=status,
            attempts=attempts,
            last_error=last_error,
            update=update,
        )

    chunks = data.get("chunks")
    files = data.get("files")
    sent_chunks = data.get("sent_chunks", [])
    sent_files = data.get("sent_files", [])
    key = data.get("key")
    if not isinstance(chunks, list) or not all(isinstance(item, str) for item in chunks):
        raise CorruptRecordError("outbox chunks are invalid")
    if not isinstance(files, list) or not all(isinstance(item, str) for item in files):
        raise CorruptRecordError("outbox files are invalid")
    if not isinstance(sent_chunks, list) or not all(isinstance(item, int) for item in sent_chunks):
        raise CorruptRecordError("sent chunk indexes are invalid")
    if not isinstance(sent_files, list) or not all(isinstance(item, str) for item in sent_files):
        raise CorruptRecordError("sent file paths are invalid")
    if key is not None and not isinstance(key, str):
        raise CorruptRecordError("outbox key is invalid")
    sent_chunk_set = frozenset(sent_chunks)
    sent_file_set = frozenset(sent_files)
    if any(index < 0 or index >= len(chunks) for index in sent_chunk_set):
        raise CorruptRecordError("sent chunk index is out of range")
    if not sent_file_set.issubset(files):
        raise CorruptRecordError("sent file is not present in the record")
    return Record(
        id=record_id,
        kind=kind,
        created_ns=created_ns,
        updated_ns=updated_ns,
        status=status,
        attempts=attempts,
        last_error=last_error,
        chunks=tuple(chunks),
        files=tuple(files),
        key=key,
        sent_chunks=sent_chunk_set,
        sent_files=sent_file_set,
    )


class _DurableStore:
    def __init__(
        self,
        directory: Path,
        kind: str,
        *,
        max_records: int = DEFAULT_MAX_RECORDS,
        max_bytes: int = DEFAULT_MAX_BYTES,
        pending_retention_seconds: float = DEFAULT_PENDING_RETENTION_SECONDS,
        dead_letter_retention_seconds: float = DEFAULT_DEAD_LETTER_RETENTION_SECONDS,
    ) -> None:
        if max_records <= 0 or max_bytes <= 0:
            raise ValueError("max_records and max_bytes must be positive")
        if pending_retention_seconds < 0 or dead_letter_retention_seconds < 0:
            raise ValueError("retention periods cannot be negative")

        self.directory = Path(directory).expanduser()
        self.kind = kind
        self.pending_directory = self.directory / kind
        self.dead_letter_directory = self.directory / "dead-letter" / kind
        self.max_records = int(max_records)
        self.max_bytes = int(max_bytes)
        self.pending_retention_seconds = float(pending_retention_seconds)
        self.dead_letter_retention_seconds = float(dead_letter_retention_seconds)
        self._thread_lock = threading.RLock()

        self.pending_directory.mkdir(parents=True, exist_ok=True)
        self.dead_letter_directory.mkdir(parents=True, exist_ok=True)
        if (
            os.stat(self.pending_directory).st_dev
            != os.stat(self.dead_letter_directory).st_dev
        ):
            raise DurableError("pending and dead-letter directories are on different filesystems")
        with self._thread_lock:
            self._maintain()

    @staticmethod
    def _validate_record_id(record_id: str) -> str:
        value = str(record_id)
        if not value or value in {".", ".."} or Path(value).name != value:
            raise ValueError("invalid record id")
        return value

    def _pending_path(self, record_id: str) -> Path:
        return self.pending_directory / f"{self._validate_record_id(record_id)}.json"

    def _dead_path(self, record_id: str) -> Path:
        return self.dead_letter_directory / f"{self._validate_record_id(record_id)}.json"

    @staticmethod
    def _read_data(path: Path) -> dict[str, Any]:
        try:
            data = json.loads(path.read_text("utf-8"))
        except FileNotFoundError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CorruptRecordError(f"cannot read {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise CorruptRecordError(f"{path} does not contain a JSON object")
        return data

    def _all_storage_files(self) -> list[Path]:
        files: list[Path] = []
        for directory in (self.pending_directory, self.dead_letter_directory):
            try:
                files.extend(
                    path
                    for path in directory.iterdir()
                    if path.is_file()
                )
            except FileNotFoundError:
                continue
        return files

    def _usage(self) -> tuple[int, int]:
        count = 0
        size = 0
        for path in self._all_storage_files():
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue
            count += 1
            size += stat.st_size
        return count, size

    def _ensure_capacity(self, payload_size: int, *, replacing: Path | None = None) -> None:
        self._prune_dead_letters()
        count, size = self._usage()
        old_size = 0
        if replacing is not None:
            try:
                old_size = replacing.stat().st_size
            except FileNotFoundError:
                replacing = None
        projected_count = count if replacing is not None else count + 1
        projected_size = size - old_size + payload_size
        if projected_count > self.max_records:
            raise CapacityError(
                f"{self.kind} record limit reached "
                f"({projected_count} > {self.max_records})"
            )
        if projected_size > self.max_bytes:
            raise CapacityError(
                f"{self.kind} byte limit reached "
                f"({projected_size} > {self.max_bytes})"
            )

    def _write_record(self, path: Path, data: dict[str, Any], *, replacing: bool = False) -> None:
        payload = _json_bytes(data)
        self._ensure_capacity(
            len(payload),
            replacing=path if replacing else None,
        )
        _atomic_write_bytes(path, payload)

    def _quarantine_corrupt(self, path: Path, error: Exception) -> None:
        destination = self.dead_letter_directory / (
            f"{path.stem}.corrupt-{uuid.uuid4().hex}"
        )
        try:
            os.replace(path, destination)
            _fsync_directory(self.pending_directory)
            _fsync_directory(self.dead_letter_directory)
        except FileNotFoundError:
            return
        log.error("quarantined corrupt %s record %s: %s", self.kind, path, error)

    def _move_to_dead(self, path: Path, reason: str) -> None:
        try:
            data = self._read_data(path)
            record = _record_from_data(data, self.kind)
        except FileNotFoundError:
            return
        except CorruptRecordError as exc:
            self._quarantine_corrupt(path, exc)
            return
        now = time.time_ns()
        data["status"] = "dead"
        data["last_error"] = _safe_reason(reason)
        data["updated_ns"] = now
        # If the process dies after this write but before the move, maintenance sees
        # status=dead and completes the transition on the next start.
        self._write_record(path, data, replacing=True)
        destination = self._dead_path(record.id)
        try:
            os.replace(path, destination)
        except FileNotFoundError:
            return
        _fsync_directory(self.pending_directory)
        _fsync_directory(self.dead_letter_directory)

    def _cleanup_stale_temps(self, now: float) -> None:
        for directory in (self.pending_directory, self.dead_letter_directory):
            for path in directory.glob(".*.tmp"):
                try:
                    if now - path.stat().st_mtime >= STALE_TEMP_SECONDS:
                        path.unlink()
                        _fsync_directory(directory)
                except FileNotFoundError:
                    continue

    def _prune_dead_letters(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        changed = False
        try:
            paths = list(self.dead_letter_directory.iterdir())
        except FileNotFoundError:
            return
        for path in paths:
            if not path.is_file() or path.name.endswith(".tmp"):
                continue
            try:
                expired = (
                    now - path.stat().st_mtime
                    >= self.dead_letter_retention_seconds
                )
                if expired:
                    path.unlink()
                    changed = True
            except FileNotFoundError:
                continue
        if changed:
            _fsync_directory(self.dead_letter_directory)

    def _maintain(self) -> None:
        now = time.time()
        self._cleanup_stale_temps(now)
        self._prune_dead_letters(now)
        try:
            paths = list(self.pending_directory.glob("*.json"))
        except FileNotFoundError:
            return
        for path in paths:
            try:
                data = self._read_data(path)
                record = _record_from_data(data, self.kind)
                if record.status == "dead":
                    self._move_to_dead(
                        path,
                        record.last_error or "dead-letter recovery",
                    )
                    continue
                if (
                    now - path.stat().st_mtime
                    >= self.pending_retention_seconds
                ):
                    self._move_to_dead(path, "pending retention expired")
            except FileNotFoundError:
                continue
            except CorruptRecordError as exc:
                self._quarantine_corrupt(path, exc)

    def _pending_records(self) -> list[Record]:
        self._maintain()
        records: list[Record] = []
        for path in self.pending_directory.glob("*.json"):
            try:
                record = _record_from_data(self._read_data(path), self.kind)
            except FileNotFoundError:
                continue
            except CorruptRecordError as exc:
                self._quarantine_corrupt(path, exc)
                continue
            if record.status == "pending":
                records.append(record)
            else:
                self._move_to_dead(path, record.last_error or "dead-letter recovery")
        records.sort(key=lambda item: (item.created_ns, item.id))
        return records

    def _fail(self, record_id: str, reason: str) -> int:
        path = self._pending_path(record_id)
        data = self._read_data(path)
        record = _record_from_data(data, self.kind)
        if record.status != "pending":
            raise InvalidTransitionError(f"record {record_id} is not pending")
        attempts = record.attempts + 1
        data["attempts"] = attempts
        data["last_error"] = _safe_reason(reason)
        data["updated_ns"] = time.time_ns()
        self._write_record(path, data, replacing=True)
        return attempts

    def _give_up(self, record_id: str, reason: str) -> None:
        self._move_to_dead(self._pending_path(record_id), reason)

    def dead_letters(self) -> list[Record]:
        with self._thread_lock:
            self._prune_dead_letters()
            records: list[Record] = []
            for path in self.dead_letter_directory.glob("*.json"):
                try:
                    records.append(
                        _record_from_data(self._read_data(path), self.kind)
                    )
                except FileNotFoundError:
                    continue
                except CorruptRecordError:
                    # Corrupt bytes remain in dead-letter for forensic inspection.
                    continue
            records.sort(key=lambda item: (item.updated_ns, item.id))
            return records


class DurableInbox(_DurableStore):
    """Durable FIFO for Telegram updates.

    ``directory`` is the bot-specific state root.  The existing
    ``directory / "offset"`` file remains the canonical offset, so upgrading does
    not discard the old value.
    """

    def __init__(
        self,
        directory: Path,
        *,
        max_records: int = DEFAULT_MAX_RECORDS,
        max_bytes: int = DEFAULT_MAX_BYTES,
        pending_retention_seconds: float = DEFAULT_PENDING_RETENTION_SECONDS,
        dead_letter_retention_seconds: float = DEFAULT_DEAD_LETTER_RETENTION_SECONDS,
    ) -> None:
        super().__init__(
            directory,
            "inbox",
            max_records=max_records,
            max_bytes=max_bytes,
            pending_retention_seconds=pending_retention_seconds,
            dead_letter_retention_seconds=dead_letter_retention_seconds,
        )
        self.offset_path = self.directory / "offset"

    def reserve(self, update: dict) -> str:
        if not isinstance(update, dict):
            raise TypeError("update must be a dict")
        try:
            update_id = int(update["update_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("update must contain an integer update_id") from exc
        if update_id < 0:
            raise ValueError("update_id cannot be negative")
        record_id = f"in-{update_id:020d}"
        path = self._pending_path(record_id)
        with self._thread_lock:
            if path.exists():
                existing = _record_from_data(self._read_data(path), "inbox")
                if existing.update != update:
                    raise RecordConflictError(
                        f"update_id {update_id} is already reserved with different content"
                    )
                return record_id
            now = time.time_ns()
            data = {
                "version": SCHEMA_VERSION,
                "kind": "inbox",
                "id": record_id,
                "created_ns": now,
                "updated_ns": now,
                "status": "pending",
                "attempts": 0,
                "last_error": None,
                "update": update,
            }
            self._write_record(path, data)
            return record_id

    def pending(self) -> list[Record]:
        with self._thread_lock:
            return self._pending_records()

    def done(self, record_id: str) -> None:
        with self._thread_lock:
            path = self._pending_path(record_id)
            try:
                path.unlink()
            except FileNotFoundError:
                return
            _fsync_directory(self.pending_directory)

    def fail(self, record_id: str, reason: str) -> int:
        with self._thread_lock:
            return self._fail(record_id, reason)

    def give_up(self, record_id: str, reason: str) -> None:
        with self._thread_lock:
            self._give_up(record_id, reason)

    def load_offset(self) -> int:
        """Read both the current JSON offset and a legacy raw integer if encountered."""

        with self._thread_lock:
            try:
                raw = self.offset_path.read_text("utf-8").strip()
            except FileNotFoundError:
                return 0
            try:
                decoded = json.loads(raw)
                value = decoded["offset"] if isinstance(decoded, dict) else decoded
                offset = int(value)
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                raise CorruptRecordError(
                    f"legacy offset file {self.offset_path} is invalid"
                ) from exc
            if offset < 0:
                raise CorruptRecordError("offset cannot be negative")
            return offset

    @property
    def offset(self) -> int:
        return self.load_offset()

    def save_offset(self, offset: int) -> None:
        """Persist a non-regressing offset in the legacy-compatible JSON format."""

        value = int(offset)
        if value < 0:
            raise ValueError("offset cannot be negative")
        with self._thread_lock:
            current = self.load_offset()
            _atomic_write_json(
                self.offset_path,
                {"offset": max(current, value)},
            )


class DurableOutbox(_DurableStore):
    """Durable FIFO for separately acknowledged text chunks and file paths."""

    def __init__(
        self,
        directory: Path,
        *,
        max_records: int = DEFAULT_MAX_RECORDS,
        max_bytes: int = DEFAULT_MAX_BYTES,
        pending_retention_seconds: float = DEFAULT_PENDING_RETENTION_SECONDS,
        dead_letter_retention_seconds: float = DEFAULT_DEAD_LETTER_RETENTION_SECONDS,
    ) -> None:
        super().__init__(
            directory,
            "outbox",
            max_records=max_records,
            max_bytes=max_bytes,
            pending_retention_seconds=pending_retention_seconds,
            dead_letter_retention_seconds=dead_letter_retention_seconds,
        )

    def enqueue(
        self,
        chunks: list[str],
        files: list[str],
        key: str | None,
    ) -> str:
        if not isinstance(chunks, list) or not all(isinstance(item, str) for item in chunks):
            raise TypeError("chunks must be a list of strings")
        if not isinstance(files, list):
            raise TypeError("files must be a list")
        normalized_files = []
        for path in files:
            normalized = os.fspath(path)
            if not isinstance(normalized, str):
                raise TypeError("file paths must resolve to strings")
            normalized_files.append(normalized)
        if len(set(normalized_files)) != len(normalized_files):
            raise ValueError("duplicate file paths cannot be acknowledged unambiguously")
        if key is not None and not isinstance(key, str):
            raise TypeError("key must be a string or None")
        if not chunks and not normalized_files:
            raise ValueError("an outbox record must contain a chunk or a file")

        with self._thread_lock:
            if key is not None:
                for existing in self._pending_records():
                    if existing.key != key:
                        continue
                    if (
                        existing.chunks != tuple(chunks)
                        or existing.files != tuple(normalized_files)
                    ):
                        raise RecordConflictError(
                            f"outbox key {key!r} already has different content"
                        )
                    return existing.id

            now = time.time_ns()
            record_id = f"out-{now:020d}-{uuid.uuid4().hex}"
            data = {
                "version": SCHEMA_VERSION,
                "kind": "outbox",
                "id": record_id,
                "created_ns": now,
                "updated_ns": now,
                "status": "pending",
                "attempts": 0,
                "last_error": None,
                "chunks": list(chunks),
                "files": normalized_files,
                "key": key,
                "sent_chunks": [],
                "sent_files": [],
            }
            self._write_record(self._pending_path(record_id), data)
            return record_id

    def pending(self) -> list[Record]:
        with self._thread_lock:
            return self._pending_records()

    def head(self) -> Record | None:
        with self._thread_lock:
            records = self._pending_records()
            return records[0] if records else None

    def mark_chunk_sent(self, record_id: str, index: int) -> None:
        with self._thread_lock:
            path = self._pending_path(record_id)
            data = self._read_data(path)
            record = _record_from_data(data, "outbox")
            if not isinstance(index, int) or index < 0 or index >= len(record.chunks):
                raise IndexError("chunk index is out of range")
            if index in record.sent_chunks:
                return
            first_pending = record.pending_chunks[0][0] if record.pending_chunks else None
            if index != first_pending:
                raise InvalidTransitionError(
                    f"chunk {index} cannot be confirmed before chunk {first_pending}"
                )
            data["sent_chunks"] = sorted((*record.sent_chunks, index))
            data["updated_ns"] = time.time_ns()
            self._write_record(path, data, replacing=True)

    def mark_file_sent(self, record_id: str, path: str) -> None:
        normalized = os.fspath(path)
        with self._thread_lock:
            record_path = self._pending_path(record_id)
            data = self._read_data(record_path)
            record = _record_from_data(data, "outbox")
            if normalized not in record.files:
                raise ValueError(f"file {normalized!r} is not in record {record_id}")
            if normalized in record.sent_files:
                return
            if record.pending_chunks:
                raise InvalidTransitionError("files cannot be confirmed before all chunks")
            first_pending = record.pending_files[0] if record.pending_files else None
            if normalized != first_pending:
                raise InvalidTransitionError(
                    f"file {normalized!r} cannot be confirmed before {first_pending!r}"
                )
            data["sent_files"] = [
                item
                for item in record.files
                if item in record.sent_files or item == normalized
            ]
            data["updated_ns"] = time.time_ns()
            self._write_record(record_path, data, replacing=True)

    def done(self, record_id: str) -> None:
        with self._thread_lock:
            path = self._pending_path(record_id)
            try:
                record = _record_from_data(self._read_data(path), "outbox")
            except FileNotFoundError:
                return
            if not record.complete:
                raise InvalidTransitionError(
                    f"record {record_id} still has unconfirmed chunks or files"
                )
            path.unlink()
            _fsync_directory(self.pending_directory)

    def fail(self, record_id: str, reason: str) -> int:
        with self._thread_lock:
            return self._fail(record_id, reason)

    def give_up(self, record_id: str, reason: str) -> None:
        with self._thread_lock:
            self._give_up(record_id, reason)
