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
Claude Code session on the machine — including sessions no bridge is watching, and including
one-shot ``claude -p`` runs from scripts. So it never guesses from the config alone: each bridge
publishes the transcript it currently tails next to its marker (``<state>/transcript``), and
only the bridge whose published transcript is **exactly** the payload's gets marked. The
published transcript takes precedence over the session guard; the guard (and then the
guard-less fallback) is left only for configs that publish no such file at all, e.g. an older
build. An empty published file means "this bridge publishes, nothing resolved yet" and is never
marked.

Before that, a turn end from a session whose config carried no guard marked *every* guard-less
config, so it closed Telegram turns of other bridges that were still being written (22. 10. 2026).
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
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


def _published(signal: str) -> str | None:
    """The transcript a bridge says it is tailing — written by AttachBridge._publish_transcript.

    Three outcomes, and the difference between the last two decides the routing: a path, ``""``
    for "this bridge publishes but has nothing resolved yet", and ``None`` for "no such file", the
    only case where the hook may still guess. An existing-but-unreadable file counts as
    publishing — a bridge we cannot read is not a bridge we may guess about."""
    p = Path(signal).parent / "transcript"
    try:
        if not p.exists():
            return None
        return p.read_text("utf-8").strip()
    except Exception:
        return ""


def _select_configs(cfgs: list[dict], transcript_path: str) -> list[dict]:
    """The bridges this turn end belongs to. Pure: each config carries its published transcript
    under ``_published`` (filled in by main via :func:`_published`), nothing is read here.

    A publishing bridge is marked ONLY on an exact path match. Matching the project directory
    instead was tried and dropped: `claude -p` runs from scripts (``zeptej-se-lenky``,
    ``lenka-katalog-api``, ``ai-novinky``) and two overlapping sessions in one cwd all produce
    turn ends in a directory a bridge is tailing — the original bug with one more step."""
    want = os.path.realpath(transcript_path)
    exact = []
    for c in cfgs:
        pub = (c.get("_published") or "").strip()
        if not pub:
            continue                 # no file, or nothing resolved yet → not matchable here
        try:
            if os.path.realpath(pub) == want:
                exact.append(c)
        except ValueError:
            continue                 # NUL byte etc. in one config must not break routing for all
    if exact:
        return exact

    # A bridge that published anything (even an empty file — "nothing resolved yet") is never
    # guessed at: it says which transcript is its own, and this is not it. Only configs with no
    # published file at all fall back to the old guess — a config with a session guard fires only
    # for ITS session, and if none matches, every guard-less config does.
    rest = [c for c in cfgs if c.get("_published") is None]
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
    except Exception:
        # Exit code stays 0 (a hook must never block the agent), but a hook that routes turn ends
        # must not fail silently either — stderr lands in Claude Code's hook output.
        traceback.print_exc(file=sys.stderr)
    finally:
        sys.exit(0)
