"""Cross-platform process/lock helpers — Linux and macOS, standard library only.

Why this module exists
----------------------
The bridge is deployed as *critical infrastructure* on machines nobody controls (a webinar
attendee's laptop, a cheap Linux box). The supervisor has to answer three questions reliably
on both operating systems:

  * "Is *my* bridge process actually running?" — without a stale shell that merely *mentions*
    the config path faking a positive. On 2026-07-30 a diagnostic ``until pgrep -f "…config.json"``
    kept a dead bridge looking alive for ten minutes and took Telegram down for the day. The cure
    is to match on ``argv`` **and** require the process to actually be the expected interpreter
    (its ``argv[0]``), so a shell that only names the string is excluded.
  * "How old is this process / this file?" — for stale-heartbeat and stale-lock decisions.
  * "Am I the only instance for this bot?" — two pollers on one token fight over ``getUpdates``
    (Telegram 409 Conflict). A second instance must fail fast with a clear message, not race.

Portability notes (things that bit us on macOS specifically):
  * ``ps -o etimes=`` does not exist on macOS (only ``etime`` as ``[[dd-]hh:]mm:ss``).
  * ``ps`` truncates ``args`` to terminal width unless ``-ww``; ``comm`` is capped at 16 chars.
  * ``/proc`` does not exist on macOS; where it exists (Linux) it is more reliable than ``ps``.
  * ``stat`` flags differ (``-f %m`` vs ``-c %Y``) — so we never shell out for mtime; ``os.stat``
    is identical on both.

Rules honoured here: no third-party dependency (no ``psutil``), never ``shell=True``, and every
function returns the *same answer* on both systems — not merely "doesn't crash".
"""
from __future__ import annotations

import os
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

try:
    import fcntl  # POSIX only; present on both Linux and macOS
except ImportError:  # pragma: no cover - Windows is not a target
    fcntl = None  # type: ignore[assignment]

#: Linux exposes /proc; macOS does not. Prefer /proc (canonical, no subprocess) and fall
#: back to ``ps`` only where it is missing.
_HAVE_PROC = os.path.isdir("/proc")

#: Bound every ``ps`` call so a wedged process table can't hang the supervisor.
_PS_TIMEOUT = 10


class AlreadyRunning(RuntimeError):
    """Raised by :func:`single_instance_lock` when another instance holds the lock."""


# --------------------------------------------------------------------------- #
# Process enumeration
# --------------------------------------------------------------------------- #
def _iter_processes():
    """Yield ``(pid, argv0, cmdline)`` for every visible process.

    ``argv0`` is the executable the process is actually running (used for the interpreter
    filter); ``cmdline`` is the full space-joined command line (used for the needle match).
    """
    if _HAVE_PROC:
        yield from _iter_processes_proc()
    else:
        yield from _iter_processes_ps()


def _iter_processes_proc():
    try:
        entries = os.scandir("/proc")
    except OSError:
        return
    with entries:
        for entry in entries:
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as fh:
                    raw = fh.read()
            except OSError:
                continue                      # process vanished or not ours to read
            if not raw:
                continue                      # kernel thread → no argv
            parts = raw.split(b"\0")
            while parts and parts[-1] == b"":
                parts.pop()
            if not parts:
                continue
            argv = [p.decode("utf-8", "replace") for p in parts]
            yield pid, argv[0], " ".join(argv)


def _iter_processes_ps():
    try:
        res = subprocess.run(
            ["ps", "-axww", "-o", "pid=", "-o", "command="],
            capture_output=True, text=True, timeout=_PS_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return
    for line in res.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        head, _, rest = line.partition(" ")
        if not head.isdigit():
            continue
        cmd = rest.strip()
        # macOS ps gives args as one string; argv0 ≈ first whitespace token. Interpreter paths
        # (…/python3.14, …/Python.app/Contents/MacOS/Python) contain no spaces, so this holds
        # for the only case the interpreter filter cares about.
        argv0 = cmd.split(" ", 1)[0] if cmd else ""
        yield int(head), argv0, cmd


def process_pids(argv_needle: str, interpreter: str | None = "python") -> list[int]:
    """PIDs of processes whose ``argv`` contains ``argv_needle``.

    Must ignore foreign processes that only *mention* the string on their command line —
    exactly what took Telegram down on 2026-07-30, when a diagnostic ``until pgrep -f
    "…config.json"`` (a shell) shadowed the real, dead bridge.

    The defence is ``interpreter``: the process's own ``argv[0]`` basename must contain it
    (case-insensitive). A ``zsh -c 'until pgrep …'`` has ``argv[0] == zsh`` and is dropped,
    while ``python -m agent2telegram run --config …`` (argv[0] == …/python3.x) is kept. Pass
    ``interpreter=None`` to disable the filter (loose match — reintroduces the false-positive
    risk, so callers guarding a daemon should always pass one). The caller's own PID is never
    returned, so a Python supervisor checking liveness can't count itself.
    """
    if not argv_needle:
        return []
    me = os.getpid()
    want = interpreter.lower() if interpreter else None
    found: list[int] = []
    for pid, argv0, cmdline in _iter_processes():
        if pid == me:
            continue
        if argv_needle not in cmdline:
            continue
        if want is not None and want not in os.path.basename(argv0).lower():
            continue
        found.append(pid)
    return sorted(found)


# --------------------------------------------------------------------------- #
# Ages
# --------------------------------------------------------------------------- #
def process_age_seconds(pid: int) -> float | None:
    """Seconds since *pid* started, or ``None`` if it isn't running / can't be read."""
    if _HAVE_PROC:
        return _process_age_proc(pid)
    return _process_age_ps(pid)


def _process_age_proc(pid: int) -> float | None:
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            data = fh.read().decode("utf-8", "replace")
    except OSError:
        return None
    # Field 2 (comm) may contain spaces and parentheses; skip to the last ')' and split the
    # rest. starttime is field 22 (1-based); the post-')' slice starts at field 3, so its
    # index there is 22 - 3 = 19. (man proc, /proc/[pid]/stat.)
    rparen = data.rfind(")")
    if rparen == -1:
        return None
    fields = data[rparen + 1:].split()
    try:
        starttime_ticks = int(fields[19])
    except (IndexError, ValueError):
        return None
    try:
        hz = os.sysconf("SC_CLK_TCK") or 100
    except (ValueError, OSError):
        hz = 100
    try:
        with open("/proc/uptime") as fh:
            uptime = float(fh.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None
    age = uptime - (starttime_ticks / hz)
    return age if age >= 0 else 0.0


def _process_age_ps(pid: int) -> float | None:
    try:
        res = subprocess.run(
            ["ps", "-o", "etime=", "-p", str(pid)],
            capture_output=True, text=True, timeout=_PS_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None                            # no such pid
    return _parse_etime(res.stdout.strip())


def _parse_etime(text: str) -> float | None:
    """Parse BSD/macOS ``ps`` elapsed time ``[[dd-]hh:]mm:ss`` into seconds."""
    text = text.strip()
    if not text:
        return None
    days = 0
    if "-" in text:
        day_part, _, text = text.partition("-")
        try:
            days = int(day_part)
        except ValueError:
            return None
    try:
        bits = [int(b) for b in text.split(":")]
    except ValueError:
        return None
    if len(bits) == 3:
        hours, minutes, seconds = bits
    elif len(bits) == 2:
        hours, minutes, seconds = 0, bits[0], bits[1]
    else:
        return None
    return float(days * 86400 + hours * 3600 + minutes * 60 + seconds)


def file_age_seconds(path) -> float | None:
    """Seconds since *path* was last modified, or ``None`` if it doesn't exist.

    Pure stdlib on purpose: ``os.stat`` is byte-identical on Linux and macOS, unlike ``stat(1)``
    whose mtime flag differs (``-c %Y`` vs ``-f %m``).
    """
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        return None
    age = time.time() - mtime
    return age if age >= 0 else 0.0


# --------------------------------------------------------------------------- #
# Single-instance lock
# --------------------------------------------------------------------------- #
@contextmanager
def single_instance_lock(path):
    """Exclusive lock held for the life of the process.

    A second instance guarding the same bot must exit with a clear message instead of racing
    the first over ``getUpdates`` (Telegram 409 Conflict — audit finding S3). Implemented with
    ``flock(2)`` (advisory, POSIX): the kernel drops the lock automatically if the holder dies,
    so a crashed instance never wedges the next start.

    Raises :class:`AlreadyRunning` if the lock is already held. (Caveat: ``flock`` is unreliable
    over NFS — keep the lock file on a local filesystem, e.g. the per-bridge state dir.)
    """
    if fcntl is None:  # pragma: no cover - non-POSIX not a target
        raise AlreadyRunning("file locking unavailable on this platform")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise AlreadyRunning(f"another instance already holds {path}") from exc
    try:
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        yield handle
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()
