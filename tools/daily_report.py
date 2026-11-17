#!/usr/bin/env python3
"""Daily bridge traffic report — measured from REAL conversations, not a synthetic canary.

A canary message was deliberately rejected as the health signal: it sends the same short text
every time, so it passes even when everything interesting is broken. Real traffic is the harder
test — long replies split into parts, attachments, voice notes, ten-minute turns, messages
arriving in the middle of unfinished work.

The most valuable detector is the user: when someone writes "are you there?" or "I never got a
reply", that is an admitted outage. Those lines can be found in the log and matched against what
the bridge was doing at that moment. The phrases are configurable, because they depend on the
language the bridge is used in — see COMPLAINT_PHRASES.

Run it daily and deliver the output through `agent2telegram notify`: a turn started from cron has
no path back to the chat on its own, so printing is not delivering.

Usage:
    python3 daily_report.py [--log PATH] [--day YYYY-MM-DD] [--days N]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta

STATE = os.path.expanduser(os.environ.get("AGENT2TELEGRAM_STATE")
                           or "~/.local/state/agent2telegram")
#: The bridge log. Installs put it in different places, so it is an env var or --log; the
#: fallback only covers the default single-install layout.
LOG = os.path.expanduser(os.environ.get("AGENT2TELEGRAM_LOG") or os.path.join(STATE, "run.log"))

# The date is optional: lines written before 2026-08-04 carry only a time.
TS = re.compile(r"^(?:(\d{4}-\d{2}-\d{2})\s+)?(\d{2}):(\d{2}):(\d{2})\s+(\w+)\s+")
TURN_START = "TURN START"
TURN_END = "TURN END"
#: The bridge logs a send in five different ways and the wording changed over time. Counting one
#: exact form means counting zero — which is exactly what happened after the move to
#: `(delivered)`: the report claimed "0 messages" on days when more than fifty went through.
#: Hence the shared prefix rather than any single variant.
FWD = "FWD ("

#: Phrases the user types when something did not arrive. This is the most accurate measure
#: available — it is the user's own experience, not our metric — but it is language-specific.
#: Override it by writing a JSON list of lowercase phrases to <state>/complaint_phrases.json.
COMPLAINT_PHRASES = [
    "are you there", "did not get", "didn't get", "never arrived", "no reply",
    "not responding", "you alive", "still working", "any update", "hello?",
]


def _load_complaint_phrases() -> list[str]:
    try:
        with open(os.path.join(STATE, "complaint_phrases.json"), encoding="utf-8") as f:
            phrases = json.load(f)
        if isinstance(phrases, list) and all(isinstance(p, str) for p in phrases):
            return [p.lower() for p in phrases]
    except (OSError, ValueError):
        pass
    return COMPLAINT_PHRASES


def _time_of(line: str):
    m = TS.match(line)
    if not m:
        return None
    h, mi, s = int(m.group(2)), int(m.group(3)), int(m.group(4))
    return h * 3600 + mi * 60 + s


def _date_of(line: str) -> str | None:
    """The line's date, or None for older lines that do not carry one yet."""
    m = TS.match(line)
    return m.group(1) if m else None


def select_days(lines: list[str], days: int, day: str | None = None) -> tuple[list[str], str]:
    """Pick the lines for the last `days` days, or for one specific `day`.

    Also returns a sentence describing what was actually measured. Before 2026-08-04 the log had
    no date, so older lines cannot be selected — better to say so out loud than to quietly mix
    them in and pass a monthly total off as a daily one, which is exactly what used to happen.
    """
    dated = [r for r in lines if _date_of(r)]
    if not dated:
        return lines, ("⚠️ The log has no dates yet, so this is NOT a daily figure — "
                       f"it is the total over the last {len(lines)} lines.")
    available = sorted({_date_of(r) for r in dated})
    if day:
        wanted = {day}
        described = f"Measured day: {day}."
    else:
        wanted = set(available[-days:])
        described = (f"Measured range: {min(wanted)} to {max(wanted)}"
                     + (f" ({len(wanted)} days)." if len(wanted) > 1 else "."))
    selected = [r for r in dated if _date_of(r) in wanted]
    if not selected:
        return [], f"No lines in the log for {day or 'the selected range'}."
    if len(dated) < len(lines):
        described += f" Skipped {len(lines) - len(dated)} older lines with no date."
    return selected, described


def read_log(path: str) -> list[str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().splitlines()
    except OSError as e:
        print(f"cannot read the log: {e}", file=sys.stderr)
        return []


def analyse(lines: list[str]) -> dict:
    """Count the metrics over the given lines. Range selection is `select_days()`."""
    v = {
        "turns": 0, "replies": 0, "turns_without_reply": 0,
        "inject_failed": 0, "inject_after_retry": 0,
        "net_routine": 0, "net_errors": 0, "backstop": 0, "abandoned_files": 0,
        "longest_turn": 0.0, "turns_over_2min": 0,
        "restarts": 0, "queue_retries": 0,
    }
    durations = []
    start = None
    saw_fwd = False
    for r in lines:
        if TURN_START in r:
            if start is not None and not saw_fwd:
                v["turns_without_reply"] += 1
            v["turns"] += 1
            start = _time_of(r)
            saw_fwd = False
        elif FWD in r:
            v["replies"] += 1
            saw_fwd = True
        elif TURN_END in r:
            m = re.search(r"dur=([\d.]+)s", r)
            if m:
                d = float(m.group(1))
                durations.append(d)
                v["longest_turn"] = max(v["longest_turn"], d)
                if d > 120:
                    v["turns_over_2min"] += 1
            if not saw_fwd:
                v["turns_without_reply"] += 1
            start = None
        elif "inject failed" in r:
            v["inject_failed"] += 1
        elif "inject succeeded on attempt" in r:
            v["inject_after_retry"] += 1
        elif "Attach bridge live" in r:
            v["restarts"] += 1
        elif "TURN END backstop" in r:
            v["backstop"] += 1
        elif "gave up after" in r:
            v["abandoned_files"] += 1
        elif "re-delivery still failing" in r:
            v["queue_retries"] += 1
        elif "Connection reset by peer" in r:
            # A long poll (50 s) closed by the other side. A day has ~1700 cycles, so this is
            # ORDINARY protocol traffic, not an incident. Reporting it as a "network outage"
            # drowns the real errors in noise.
            v["net_routine"] += 1
        elif "urlopen error" in r or "HTTP 409" in r or "HTTP 502" in r or "timed out" in r:
            v["net_errors"] += 1
    if durations:
        durations.sort()
        v["median_turn"] = durations[len(durations) // 2]
        v["p90_turn"] = durations[int(len(durations) * 0.9)]
    return v


STATE_FILE = os.path.join(STATE, "daily_report_state.json")

#: Metrics worth reporting as a CHANGE against the previous run. Key = name used in `analyse()`,
#: value = (human-readable name, how far it must move to be worth mentioning).
TRACKED = {
    "inject_failed": ("failed write into the agent window", 1),
    "backstop": ("backstop firing", 1),
    "queue_retries": ("repeated failed send", 1),
    "abandoned_files": ("abandoned attachment", 1),
    "restarts": ("bridge restart", 5),
    "net_errors": ("genuine network error", 20),
    "turns_over_2min": ("turn longer than two minutes", 10),
}


#: How many replies must go through before a percentage means anything. Below this, "100 %" only
#: proves that nothing happened — and that is precisely the number the report used to show while
#: it was counting messages by a string the bridge had stopped writing.
MIN_TRAFFIC = 5

#: Penalties per phenomenon: (key, points each, cap, name). A lost message and a user complaint
#: cost the most on purpose — they are the only two things the user actually notices.
PENALTIES = (
    ("dead_letter",      34, 100, "message in dead-letter"),
    ("complaints",       25, 100, "user reported a missing reply"),
    ("abandoned_files",  20, 60,  "abandoned attachment"),
    ("inject_failed",    15, 45,  "failed write into the agent window"),
    ("queue_retries",     8, 24,  "repeated failed send"),
    ("restarts",          3, 12,  "bridge restart"),
    ("turns_over_2min",   1, 8,   "turn over two minutes"),
    ("net_errors",        1, 6,   "genuine network error"),
)


def health(v: dict, dead_letters: int, complaints: int) -> tuple[int | None, list[str]]:
    """Bridge health as a percentage, or None when there was too little traffic to judge.

    A hundred percent must mean "everything got through", not "nothing happened". Below
    `MIN_TRAFFIC` no percentage is reported at all — a quiet day is not a healthy day, it is a
    day we know nothing about. The score must be able to fall, otherwise nobody reads it.
    """
    if v.get("replies", 0) < MIN_TRAFFIC:
        return None, [f"only {v.get('replies', 0)} replies — too little to judge"]

    source = dict(v)
    source["dead_letter"] = dead_letters
    source["complaints"] = complaints

    score = 100
    reasons: list[str] = []
    for key, each, cap, name in PENALTIES:
        count = int(source.get(key, 0) or 0)
        if not count:
            continue
        penalty = min(count * each, cap)
        score -= penalty
        reasons.append(f"−{penalty} % … {name} ({count}×)")
    return max(0, score), reasons


def load_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(v: dict, dead_letters: int, health_pct: int | None = None) -> None:
    """Written through a temporary file — an interrupted run must not leave broken state."""
    data = {k: v.get(k, 0) for k in TRACKED}
    data["dead_letter"] = dead_letters
    if health_pct is not None:
        data["health"] = health_pct
    tmp = f"{STATE_FILE}.{os.getpid()}.tmp"
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, sort_keys=True)
        os.replace(tmp, STATE_FILE)
    except OSError as e:
        print(f"could not save the report state: {e}", file=sys.stderr)


def changes(v: dict, dead_letters: int, previous: dict) -> list[str]:
    """What is NEW, WORSE or RESOLVED since the previous report.

    Without this the report looks identical every day and after a week nobody reads it.
    """
    if not previous:
        return ["- First report with memory, nothing to compare against."]
    out = []
    for key, (name, threshold) in TRACKED.items():
        now, before = v.get(key, 0), previous.get(key, 0)
        delta = now - before
        if before == 0 and now > 0:
            out.append(f"- 🆕 New: {name} – {now}× (none before).")
        elif now == 0 and before > 0:
            out.append(f"- ✅ Resolved: {name} no longer happens (was {before}×).")
        elif delta >= threshold:
            out.append(f"- 🔺 Worse: {name} {before}× → {now}×.")
        elif -delta >= threshold:
            out.append(f"- 🔻 Better: {name} {before}× → {now}×.")
    dl_before = previous.get("dead_letter", 0)
    if dead_letters > dl_before:
        out.append(f"- 🆕 New: dead-letter grew by {dead_letters - dl_before} messages.")
    elif dead_letters < dl_before:
        out.append(f"- ✅ Resolved: dead-letter shrank from {dl_before} to {dead_letters}.")
    return out or ["- Nothing new, same as the previous report."]


def dead_letter() -> tuple[int, list[str]]:
    """The number of undelivered messages — the hardest number available."""
    records = []
    for root, _dirs, files in os.walk(os.path.join(STATE, "dead-letter")):
        for s in files:
            if s.endswith(".json"):
                records.append(os.path.join(root, s))
    return len(records), records[:5]


def complaints(lines: list[str]) -> list[str]:
    """The user's own messages saying something never arrived."""
    phrases = _load_complaint_phrases()
    found = []
    for r in lines:
        low = r.lower()
        if "inject" not in low and "fwd" not in low:
            continue
        for phrase in phrases:
            if phrase in low:
                found.append(r.strip()[:110])
                break
    return found


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--log", default=LOG)
    p.add_argument("--days", type=int, default=1,
                   help="how many of the most recent days to measure (default 1)")
    p.add_argument("--day", help="one specific day, YYYY-MM-DD")
    p.add_argument("--max-lines", type=int, default=200_000,
                   help="cap on lines read; day selection itself is --days/--day")
    a = p.parse_args()

    lines = read_log(a.log)[-a.max_lines:]
    if not lines:
        print("EMPTY LOG — nothing to measure (suspicious in itself)")
        return 1

    lines, range_described = select_days(lines, a.days, a.day)
    if not lines:
        print(range_described)
        return 1

    v = analyse(lines)
    dl_count, _dl_samples = dead_letter()
    previous = load_state()
    reported = complaints(lines)
    health_pct, health_reasons = health(v, dl_count, len(reported))

    # Format: an emoji HEADING with a colon, bullets underneath. No tables — they fall apart in
    # Telegram. The report is a working tool, hence the "Recommended actions" section at the end.
    losses = dl_count + v["abandoned_files"]
    r = []

    # The score belongs at the top: the point is to see at a glance how the bridge is doing.
    if health_pct is None:
        r.append("📈 Bridge health: cannot be measured")
        r.extend(f"- {d}" for d in health_reasons)
    else:
        mark = "🟢" if health_pct >= 95 else ("🟡" if health_pct >= 80 else "🔴")
        before = previous.get("health")
        trend = ""
        if isinstance(before, int) and before != health_pct:
            direction = "better" if health_pct > before else "worse"
            trend = f" (previously {before} %, {direction} by {abs(health_pct - before)})"
        r.append(f"📈 Bridge health: {mark} {health_pct} %{trend}")
        if health_reasons:
            r.extend(f"- {d}" for d in health_reasons)
        else:
            r.append("- Not a single penalty.")
    r.append("")

    if losses:
        r.append("❌ Lost messages:")
        if dl_count:
            r.append(f"- {dl_count} in dead-letter")
        if v["abandoned_files"]:
            r.append(f"- {v['abandoned_files']} abandoned attachments")
        r.append("")
    else:
        r.append("✅ Nothing lost:")
        r.append("- no message was lost")
        r.append("- dead-letter is empty")
        r.append("")

    r.append("🆕 Changes since the previous report:")
    r.extend(changes(v, dl_count, previous))
    r.append("")

    r.append("📊 Traffic:")
    r.append(f"- {range_described}")
    r.append(f"- {v['replies']} messages, {v['restarts']} restarts")
    r.append(f"- usual response time {v.get('median_turn', 0):.0f} s")
    r.append(f"- worst case {v['longest_turn']:.0f} s")
    if v["net_routine"]:
        r.append(f"- {v['net_routine']}× the connection was re-established (routine, no impact)")
    if v["net_errors"]:
        r.append(f"- {v['net_errors']} genuine network errors (DNS, 409, 502, timeout)")
    r.append("")

    friction = []
    if v["inject_failed"]:
        saved = v["inject_after_retry"]
        friction.append(f"- {v['inject_failed']}× could not write into the agent window"
                        + (f" ({saved}× saved by a retry)" if saved else " (retrying did not help)"))
    if v["backstop"]:
        friction.append(f"- {v['backstop']}× the backstop had to step in")
    if v["queue_retries"]:
        friction.append(f"- {v['queue_retries']}× a send kept failing")
    if friction:
        r.append("⚠️ Friction:")
        r.extend(friction)
        r.append("")

    if reported:
        r.append("🔔 The user reported a problem:")
        r.append(f"- {len(reported)}× someone wrote that something never arrived")
        r.append("- this takes precedence over everything else")
        r.append("")

    # Numbered so the reader can answer "fix 1 and 3". Without numbers the only possible
    # answer is "fix everything".
    actions = []
    if losses:
        actions.append("Find the cause of the undelivered messages and drain dead-letter.")
    if reported:
        actions.append("Read the log around the times someone said a reply never arrived.")
    if v["inject_failed"] and not v["inject_after_retry"]:
        actions.append("Writing into the agent window fails and retrying does not help — find out why.")
    if v["restarts"] > 5:
        actions.append(f"{v['restarts']} restarts is a lot, track down the cause.")
    if v["queue_retries"]:
        actions.append("Inspect the records that kept failing to send.")
    if v["net_errors"] > 50:
        actions.append(f"{v['net_errors']} network errors is above the usual level — check DNS and VPN.")
    if v["backstop"]:
        actions.append("Review the turns where the backstop had to step in — a reply was missing there.")

    r.append("🔧 Recommended actions:")
    if actions:
        for i, text in enumerate(actions, 1):
            r.append(f"- {i}. {text}")
        r.append("- Reply with the numbers to fix.")
    else:
        r.append("- None, traffic is clean.")
    r.append("")

    print("\n".join(_tidy_bullets(r)))
    # State is saved after printing — if the report crashes, tomorrow compares against the
    # same baseline rather than a half-written one.
    save_state(v, dl_count, health_pct)
    return 0


def _tidy_bullets(lines: list[str]) -> list[str]:
    """Every bullet starts with a capital letter and ends with a full stop.

    Done here once for all lines rather than by hand at each call site — otherwise it would
    sooner or later slip somewhere and the format would drift.
    """
    done = []
    for line in lines:
        if line.startswith("- ") and len(line) > 2:
            text = line[2:]
            text = text[0].upper() + text[1:]
            if text[-1] not in ".!?:":
                text += "."
            line = "- " + text
        done.append(line)
    return done


if __name__ == "__main__":
    sys.exit(main())
