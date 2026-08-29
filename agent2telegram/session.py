"""Persistent agent sessions over **tmux** — the same approach as a hand-rolled bridge that
drives a live TUI, generalized into a product.

Why tmux: it keeps the agent's *interactive* session alive, so full context, loaded tools
and working state persist across messages — exactly like talking to it in a terminal. It
works for any agent that has an interactive CLI (Claude Code, Codex, …).

Inbound (proven send-keys sequence): clear the prompt line (``C-u``), type the message
literally (``send-keys -l --``), then submit (``Enter``). Newlines are collapsed so a single
Enter submits the whole message.

Completion + response — two strategies:
  * **Hook** (robust, used for Claude Code): the agent runs a Stop hook at end of turn that
    writes the final answer to a per-session signal file; the bridge waits for it. This is
    authoritative (reads the transcript, not the screen). Set up by the installer.
  * **Idle** (universal fallback): poll ``capture-pane`` and treat the turn as done once the
    output has been stable for ``idle`` seconds. Good enough for agents without a hook.
"""
from __future__ import annotations

import logging
import re
import shlex
import shutil
import subprocess
import time
import uuid
from pathlib import Path

log = logging.getLogger("agent2telegram.session")

# TUI chrome filters for the idle (screen-scraping) path. capture-pane gives plain text
# (no ANSI), so we only strip recognizable decoration lines and leading bullet markers.
_SEP_RE = re.compile(r"^[\s─━—–_=·.\-]{6,}$")
_STATUS_RE = re.compile(r"^[✻✶✳✢✺✷*]\s")           # spinner/status, e.g. "✻ Worked for 1s"
_BULLET_RE = re.compile(r"^\s*[⏺●○•]\s?")           # assistant output bullet

MAX_TMUX_INJECTION_CHARS = 8000

#: How long to wait for the TUI to acknowledge the Enter before calling the injection stuck,
#: and how often to look. `tmux send-keys` exits 0 as soon as tmux has written the keys into
#: the pane — it says nothing about whether the program on the other end acted on them, so
#: without this check "the message was delivered" is a claim, not a measurement.
#: ⚠️ The wait is generous on purpose: a long prompt wraps over several rows and the TUI
#: redraw was measured still lagging half a second behind. Declaring failure early would be
#: worse than not checking at all.
SUBMIT_CONFIRM_S = 2.5
SUBMIT_POLL_S = 0.15
#: How much of the injected text has to be gone from the prompt for it to count as submitted.
#: The tail, not the head: a wrapped prompt puts the cursor on the LAST row, so that is the
#: part that is visible there.
SUBMIT_TAIL_CHARS = 24
_SHELL_COMMANDS = {
    "ash", "bash", "csh", "dash", "fish", "ksh", "mksh", "pwsh", "sh", "tcsh", "zsh",
}
_AGENT_WRAPPERS = {
    "bun", "deno", "node", "nodejs", "npm", "npx", "pnpm", "python", "python3", "uv",
    "uvx", "yarn",
}


def _clean_tui(text: str) -> str:
    out = []
    for line in text.splitlines():
        s = line.rstrip()
        if not s.strip():
            continue
        if _SEP_RE.match(s):
            continue
        if s.lstrip().startswith("❯") or s.lstrip().startswith(">"):   # prompt / input echo
            continue
        if _STATUS_RE.match(s.lstrip()):
            continue
        out.append(_BULLET_RE.sub("", s))
    return "\n".join(out).strip()


def sanitize_for_tmux(text: str) -> str:
    """Drop terminal control bytes that tmux would otherwise pass to the live TUI."""
    out = []
    for ch in text:
        codepoint = ord(ch)
        if (
            ch not in "\n\t"
            and (codepoint < 0x20 or codepoint == 0x7f or 0x80 <= codepoint <= 0x9f)
        ):
            continue
        out.append(ch)
        if len(out) >= MAX_TMUX_INJECTION_CHARS:
            break
    return "".join(out)


def _command_name(command: str) -> str:
    return Path(str(command).strip()).name.lower().lstrip("-")


def _argv0(args: str) -> str:
    if not args:
        return ""
    try:
        parts = shlex.split(args)
    except ValueError:
        parts = str(args).split()
    return parts[0] if parts else ""


def _command_names(commands: list[str] | tuple[str, ...] | set[str]) -> tuple[str, ...]:
    out: list[str] = []
    seen = set()
    for command in commands:
        name = _command_name(command)
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return tuple(out)


def _mentions_expected_agent(args: str, expected: tuple[str, ...]) -> bool:
    low = (args or "").lower()
    for name in expected:
        if re.search(rf"(?<![a-z0-9]){re.escape(name)}(?![a-z0-9])", low):
            return True
    return False


def _process_command_names(command: str, args: str) -> tuple[str, ...]:
    out: list[str] = []
    seen = set()
    for value in (command, _argv0(args)):
        name = _command_name(value)
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return tuple(out)


def _process_matches_agent(command: str, args: str, expected: tuple[str, ...]) -> bool:
    names = _process_command_names(command, args)
    if any(name in expected for name in names):
        return True
    return any(name in _AGENT_WRAPPERS for name in names) and _mentions_expected_agent(args, expected)


def _process_shell_name(command: str, args: str) -> str:
    for name in _process_command_names(command, args):
        if name in _SHELL_COMMANDS:
            return name
    return ""


def _pane_value(target: str, fmt: str) -> str:
    res = _tmux("display-message", "-p", "-t", target, fmt, check=False, timeout=3)
    return (res.stdout or "").strip() if res.returncode == 0 else ""


def _pane_processes(target: str) -> list[tuple[int, int, str, str]]:
    pid_s = _pane_value(target, "#{pane_pid}")
    if not pid_s.isdigit():
        return []
    root_pid = int(pid_s)
    try:
        proc = subprocess.run(
            ["ps", "-Ao", "pid=,ppid=,comm=,args="],
            capture_output=True,
            text=True,
            check=False,
            timeout=3,
        )
    except Exception:
        return []
    if proc.returncode != 0:
        return []

    rows: dict[int, tuple[int, int, str, str]] = {}
    children: dict[int, list[int]] = {}
    for line in (proc.stdout or "").splitlines():
        parts = line.strip().split(None, 3)
        if len(parts) < 3:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        command = parts[2]
        args = parts[3] if len(parts) > 3 else command
        rows[pid] = (pid, ppid, command, args)
        children.setdefault(ppid, []).append(pid)

    found: list[tuple[int, int, str, str]] = []
    stack = [root_pid]
    seen = set()
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        row = rows.get(pid)
        if row:
            found.append(row)
        stack.extend(children.get(pid, ()))
    return found


def _agent_alive(target: str, expected_commands: list[str] | tuple[str, ...] | set[str]) -> tuple[bool, str]:
    expected = _command_names(expected_commands)
    if not expected:
        return True, "no expected agent command configured"

    current = _pane_value(target, "#{pane_current_command}")
    current_name = _command_name(current)
    expected_s = ", ".join(expected)
    if current_name in expected:
        return True, f"tmux pane command is {current_name}"

    # Security guard: send-keys must only target a live agent TUI. tmux may report a
    # stale title or a login shell as the pane command, so first search the pane's full
    # process subtree for the configured agent binary by basename.
    processes = _pane_processes(target)
    for _pid, _ppid, command, args in processes:
        if _process_matches_agent(command, args, expected):
            return True, f"tmux pane process is {_command_name(command)}"

    shell_name = current_name if current_name in _SHELL_COMMANDS else ""
    if not shell_name and len(processes) == 1:
        _pid, _ppid, command, args = processes[0]
        shell_name = _process_shell_name(command, args)
    if shell_name:
        return False, f"tmux pane is at a shell prompt ({shell_name}); expected {expected_s}"

    detail = current_name or "unknown"
    return False, f"tmux pane command is {detail}; expected {expected_s}"


class SessionError(Exception):
    pass


def _tmux(*args: str, check: bool = True, timeout: float = 10) -> subprocess.CompletedProcess:
    return subprocess.run(["tmux", *args], capture_output=True, text=True, check=check, timeout=timeout)


class TmuxSession:
    """A live agent running in a detached tmux session, fed via send-keys."""

    def __init__(self, agent_argv: list[str], *, cwd: Path, name: str | None = None,
                 timeout: int = 600, idle: float = 1.5, settle: float = 0.4,
                 origin_prefix: str = "", signal_file: Path | None = None,
                 boot_wait: float = 2.0,
                 expected_agent_commands: list[str] | tuple[str, ...] | None = None) -> None:
        if shutil.which("tmux") is None:
            raise SessionError("tmux is not installed. Install it first: `sudo apt install tmux` (Debian/Ubuntu) or `brew install tmux` (macOS) — "
                               "or `apt install tmux`) — the persistent session needs it.")
        self.name = name or ("a2t_" + uuid.uuid4().hex[:10])
        self._timeout = timeout
        self._idle = idle
        self._settle = settle
        self._origin = origin_prefix
        self._signal = signal_file
        self._expected_agent_commands = tuple(expected_agent_commands or (agent_argv[:1] if agent_argv else ()))
        cwd.mkdir(parents=True, exist_ok=True)
        if not self._exists():
            if not agent_argv:
                # Attach mode: we only drive an existing session, never create one.
                raise SessionError(f"tmux session '{self.name}' does not exist (attach mode).")
            _tmux("new-session", "-d", "-s", self.name, "-x", "220", "-y", "50", *agent_argv,
                  timeout=15)
            time.sleep(boot_wait)   # let the TUI come up before the first message

    # ---- lifecycle ---------------------------------------------------------
    def _exists(self) -> bool:
        return subprocess.run(["tmux", "has-session", "-t", self.name],
                              capture_output=True).returncode == 0

    @property
    def alive(self) -> bool:
        return self._exists()

    def close(self) -> None:
        _tmux("kill-session", "-t", self.name, check=False)

    # ---- messaging ---------------------------------------------------------
    def _pane_ok(self) -> tuple[bool, str]:
        return _agent_alive(self.name, getattr(self, "_expected_agent_commands", ()))

    def _send_keys(self, text: str) -> None:
        ok, detail = self._pane_ok()
        if not ok:
            # Fail closed: if the agent has crashed back to a shell, Enter would execute the
            # Telegram message as a shell command instead of submitting it to the agent TUI.
            log.warning("refusing tmux injection into '%s': %s", self.name, detail)
            raise SessionError(f"refusing to inject into tmux session '{self.name}': {detail}")
        text = sanitize_for_tmux(text)
        text = " ".join(text.splitlines())                 # one Enter submits everything
        if self._origin:
            text = f"{self._origin}{text}"
        text = sanitize_for_tmux(text)
        _tmux("send-keys", "-t", self.name, "C-u"); time.sleep(0.05)
        _tmux("send-keys", "-t", self.name, "-l", "--", text); time.sleep(0.15)
        _tmux("send-keys", "-t", self.name, "Enter")
        self._confirm_submitted(text)

    def _confirm_submitted(self, text: str) -> None:
        """Make sure the Enter actually submitted, and shout when it did not.

        ⛔ Why this exists. `tmux send-keys` returning 0 only means tmux delivered the
        keystroke; the message can still be left sitting in the prompt, unsent. That failure
        is completely silent — the bridge logs the message as delivered, the user waits for an
        answer that will never come, and the only trace is text visible in a tmux window
        nobody is looking at. It cost an hour of diagnosis on 2026-08-28.

        ⚠️ Deliberately NOT a fix for whatever swallows the Enter: the cause is not measured
        (12 of 12 and then 12 of 12 injections landed on this machine, with and without a 3x
        CPU overload, on Claude Code 2.1.235). This turns a silent failure into a loud one.
        One extra Enter is tried first because it is free and cannot duplicate anything — an
        Enter on an empty prompt is a no-op, and if the text is still there it was never sent.
        """
        stav = self._submitted(text)
        if stav is not False:
            return                          # submitted, or not measurable — either way, no claim
        log.warning("the prompt still holds the message after Enter in '%s' — trying once more",
                    self.name)
        _tmux("send-keys", "-t", self.name, "Enter", check=False)
        if self._submitted(text) is False:
            raise SessionError(
                f"the message stayed in the prompt of tmux session '{self.name}' — "
                "Enter did not submit it")
        log.info("the second Enter submitted it in '%s'", self.name)

    def _capture(self) -> str:
        return _tmux("capture-pane", "-p", "-t", self.name, check=False).stdout

    def _prompt_row(self) -> str | None:
        """The row the cursor is on — the prompt line, whatever the TUI draws around it.

        The cursor is the one place in the pane that cannot be confused with the transcript:
        the agent writes its answers upwards, the cursor stays in the prompt. An echo of an
        already-submitted message therefore never sits under it. Returns None when tmux won't
        say (then the caller must not conclude anything).
        """
        r = _tmux("display-message", "-p", "-t", self.name, "#{cursor_y}", check=False)
        y = (r.stdout or "").strip()
        if not y.isdigit():
            return None
        r = _tmux("capture-pane", "-p", "-t", self.name, "-S", y, "-E", y, check=False)
        if r.returncode != 0:
            return None
        return r.stdout.rstrip("\n")

    def _submitted(self, text: str) -> bool | None:
        """Did the Enter take? True/False, or None when it cannot be measured.

        Measured, not assumed: while text sits unsent in the prompt, its tail is on the cursor
        row; once it is submitted (or queued by the TUI for after the current turn), the prompt
        clears and the tail is gone from there.
        """
        tail = text[-SUBMIT_TAIL_CHARS:]
        deadline = time.monotonic() + SUBMIT_CONFIRM_S
        seen = None
        while True:
            row = self._prompt_row()
            if row is None:
                return None                 # tmux won't say — don't guess either way
            seen = tail not in row
            if seen or time.monotonic() >= deadline:
                return seen
            time.sleep(SUBMIT_POLL_S)

    def inject(self, text: str) -> None:
        """Fire-and-forget: type the message into the session, don't wait for a reply.
        Used by the async attach bridge (outbound is handled separately)."""
        if not self.alive:
            raise SessionError(f"agent session '{self.name}' is gone")
        self._send_keys(text)

    def send(self, text: str) -> str:
        if not self.alive:
            raise SessionError(f"agent session '{self.name}' is gone")
        if self._signal is not None:
            return self._send_with_hook(text)
        return self._send_with_idle(text)

    def _send_with_hook(self, text: str) -> str:
        """Authoritative completion: a Stop hook writes the final answer to the signal file."""
        try:
            self._signal.unlink()                          # clear any stale answer
        except FileNotFoundError:
            pass
        self._send_keys(text)
        deadline = time.monotonic() + self._timeout
        while time.monotonic() < deadline:
            if self._signal.exists():
                answer = self._signal.read_text("utf-8")
                try:
                    self._signal.unlink()
                except OSError:
                    pass
                return answer.strip()
            time.sleep(0.3)
        raise SessionError(f"agent timed out after {self._timeout}s")

    def _send_with_idle(self, text: str) -> str:
        before = self._capture()
        self._send_keys(text)
        deadline = time.monotonic() + self._timeout
        last, stable_since = "", 0.0
        while time.monotonic() < deadline:
            time.sleep(self._settle)
            cur = self._capture()
            if cur != last:
                last, stable_since = cur, time.monotonic()
                continue
            if stable_since and (time.monotonic() - stable_since) >= self._idle and cur != before:
                return self._delta(before, cur)
        raise SessionError(f"agent timed out after {self._timeout}s")

    @staticmethod
    def _delta(before: str, after: str) -> str:
        b, a = before.splitlines(), after.splitlines()
        i = 0
        while i < len(b) and i < len(a) and b[i] == a[i]:
            i += 1
        return _clean_tui("\n".join(a[i:]))
