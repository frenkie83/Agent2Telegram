"""Claude Code **Stop hook** for Agent2Telegram (attach mode).

Claude Code has no end-of-turn record in its transcript, so to switch the "typing…" indicator
off and clear the live status bubble at the *exact* moment a turn ends, the bridge relies on a
tiny marker file. Claude Code runs this hook at the end of every assistant turn; it writes that
marker for the matching session. (Codex needs no hook — its rollout log records ``task_complete``.)

The bridge forwards every assistant message by tailing the transcript directly, so this hook does
**not** send anything itself — it only signals "turn ended".

Register it once (the setup wizard does this) in the agent's settings, e.g. ``~/.claude/settings.json``::

    {"hooks": {"Stop": [{"hooks": [{"type": "command",
       "command": "python3 -m agent2telegram.stop_hook"}]}]}}

It reads the signal path / optional session guard from the Agent2Telegram config, so the command
line needs no arguments. Always exits 0 (never blocks the agent).

Because it is registered **once globally**, the hook runs at the end of every turn of *every*
Claude Code session on the machine — most of which no bridge is watching. So it never guesses
from the config alone: each bridge publishes the transcript it currently tails next to its
marker (``<state>/transcript``), and only the bridge whose published transcript is the one in
the payload gets marked. Guessing (session guard, then guard-less configs) is left only for
bridges that published nothing, e.g. an older build. Before that, one session's turn end marked
every bridge and closed Telegram turns that were still being written (22. 10. 2026).
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def _load_cfg() -> dict:
    try:
        from .config import config_path
        return json.loads(Path(config_path()).read_text("utf-8"))
    except Exception:
        return {}


def _all_cfgs() -> list[dict]:
    """Every bridge config in the config directory — so a single global Stop hook serves multiple
    Claude bridges (each `agent2telegram connect` writes its own <name>.json)."""
    try:
        from .config import config_path
        d = Path(config_path()).parent
    except Exception:
        return []
    out = []
    if d.is_dir():
        for p in sorted(d.glob("*.json")):
            try:
                out.append(json.loads(p.read_text("utf-8")))
            except Exception:
                pass
    return out


def _mark(signal: str) -> None:
    marker = Path(signal).parent / "turn_end"
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(str(time.time()), encoding="utf-8")
    except OSError:
        pass


def _published(signal: str) -> str:
    """The transcript a bridge says it is tailing — written by AttachBridge._publish_transcript.
    ``""`` when it published none (no file, empty, unreadable)."""
    try:
        return (Path(signal).parent / "transcript").read_text("utf-8").strip()
    except Exception:
        return ""


def _select_configs(cfgs: list[dict], transcript_path: str) -> list[dict]:
    """The bridges this turn end belongs to. Pure: each config carries its published transcript
    under ``_published`` (filled in by main via :func:`_published`), nothing is read here."""
    want = os.path.realpath(transcript_path)
    publishers = [(c, os.path.realpath((c["_published"] or "").strip()))
                  for c in cfgs if (c.get("_published") or "").strip()]

    exact = [c for c, tr in publishers if tr == want]
    if exact:
        return exact
    # Claude starts a new transcript per session, and a bridge can still be on the previous one
    # for a few seconds after a session swap. Same project dir = same cwd = our session anyway.
    same_dir = [c for c, tr in publishers
                if os.path.dirname(tr) == os.path.dirname(want)]
    if same_dir:
        return same_dir

    # A bridge that published a DIFFERENT transcript is definitely not ours — only the ones that
    # published nothing are still guessed at. A config with a session guard fires only for ITS
    # session; if none matched, fall back to the guard-less ones (old behaviour preserved).
    rest = [c for c in cfgs if not (c.get("_published") or "").strip()]
    base = os.path.basename(transcript_path)
    guarded = [c for c in rest
               if c.get("claude_session_id") and base.startswith(c["claude_session_id"])]
    if guarded:
        return guarded
    return [c for c in rest if not c.get("claude_session_id")]


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}
    path = payload.get("transcript_path")
    if not path:
        return

    cfgs = [c for c in _all_cfgs() if c.get("signal_file")]
    if not cfgs:                                   # legacy: just the env/default config
        c = _load_cfg()
        if c.get("signal_file"):
            cfgs = [c]
    for c in cfgs:
        c["_published"] = _published(c["signal_file"])

    for c in _select_configs(cfgs, path):
        _mark(c["signal_file"])


if __name__ == "__main__":
    try:
        main()
    finally:
        sys.exit(0)
