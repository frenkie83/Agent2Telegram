# Near-miss log

Modelled on aviation practice: cases that caused **no damage** get written down too. A crash
demands attention on its own; a near miss is a free warning — which is exactly why it gets
forgotten.

Every entry has to answer one question: **"what will catch this next time, by itself?"** Without
that, the entry is just an anecdote.

---

## 2026-07-31 · The two-instance lock existed but was never called

**What happened:** `single_instance_lock` was written, tested and reported as done. Nothing on the
production path called it, so the protection against two pollers (409 Conflict) did not exist.
It was reported as "two instances cannot start" — untruthfully.

**Why it slipped through:** the test exercised the **helper**, not **its wiring**. A green test
invited the conclusion that the function was in service.

**Damage:** none, it had not been deployed yet.

**What catches it next time:** every new safeguard needs a test that goes down the **production
path** (`run()`), not through a helper. The checklist question: *does anything actually call
this?* A cheap check: `grep -rn "<name>" --include=*.py | grep -v tests`.

---

## 2026-07-31 · A test passed because the process under test did not exist

**What happened:** the test was meant to verify that a foreign shell is not counted when watching
processes. The shell was started as `sh -c "sleep 30  # marker"`, but in that form the shell
**exec-replaces** itself with `sleep` and the marker disappears from the command line. The test
therefore passed because the process was not in the list at all — not because the filter had
excluded it.

**Why it slipped through:** the colour was green. Nobody asked *why* it was green.

**Damage:** none — but the same trap caught two people independently within an hour.

**What catches it next time:** a test claiming "X is not counted" must also assert that X is
**really in the list**. Otherwise it is testing an empty set.

---

## 2026-07-31 · A mutation check exposed an uncovered branch

**What happened:** mutating `if delivered is False:` into `if False:` survived. The tests covered
only a handler raising, not a silent failure — the **more common** case (a frozen tmux pane).

**Why it slipped through:** the two branches looked symmetrical, so it seemed both were covered.

**Damage:** none, the mutation check ran before deployment.

**What catches it next time:** run mutation checks as a **gate before deployment**, not as a
one-off exercise.

---

## 2026-07-31 · A stop hook promised a safety net that 50 lines of dead code did not provide

**What happened:** `stop_forward.py` has a `return` with ~50 lines of unreachable code behind it.
Its docstring — and the operator's own notes — claimed the hook would forward a forgotten reply.
The behaviour had been switched off deliberately months earlier; the documentation was never
updated.

**Damage:** none directly, but two parties were relying on a safety net that was not there.

**What catches it next time:** when a behaviour is switched off, the promise about it must
disappear in the same commit. Dead code gets deleted, not commented out — otherwise, months
later, it becomes documentation.

---

## 2026-07-31 · Fixing a blocked queue created a silent loss

**What happened:** the fix for "a broken attachment must not block the queue" called
`mark_file_sent` once the attempts ran out — recording in our own bookkeeping that the attachment
had arrived when it had not. One failure mode was solved by creating a worse one.

**Why it slipped through:** the tests checked that the queue kept moving, but not **at what cost**.
They were green.

**Damage:** none, it had not been deployed.

**What catches it next time:** every "this must not block" fix needs an assertion about **what
happened to the thing that was skipped**. Skipping is only allowed when the item is stored
somewhere it can be found. The test now checks dead-letter, and that an abandoned attachment is
not listed among the sent ones.

---

## 2026-07-31 · The mutation script deleted work in progress

**What happened:** the mutation script restores files with `git checkout --`. It was run against an
UNCOMMITTED tree, so a fresh fix was overwritten by the last commit.

**Damage:** a few minutes of work; the fix could be rewritten from context.

**What catches it next time:** mutation checks belong **after** a commit, never before. Ideally the
script should refuse to run while `git status` is dirty.

---

## 2026-07-31 · A rollback test looked exactly like an outage

**What happened:** during a planned test of returning to the previous version the bridge was down
for a minute. The user wrote "Something's broken!! You're not responding" — from their side
indistinguishable from a real outage.

**Why:** the warning had been "you might briefly lose a reply". That is far too soft. It lacked a
concrete number and any certainty.

**Damage:** none technically, but an unnecessary scare — and for a tool whose entire purpose is
"never go quiet", that is worse than it sounds.

**What catches it next time:** before EVERY intervention that interrupts delivery, send a message
in advance with a concrete duration ("it will be quiet for about a minute, this is planned, I will
be back right after"). And send it by a route the intervention will not interrupt — from a
background job, not from the turn that is about to end with the intervention.

---

## 2026-08-01 · A test tool opened DNS on every interface

**What happened:** a Linux VM runtime was started for the sake of testing. It opened a recursive
DNS resolver (`*:53`) on **every interface**, not just loopback. The nightly security audit
flagged it red.

**Why it slipped through:** the tool was started for one task and no one asked what else it opens.
A test environment was judged harmless because it "only runs tests".

**Damage:** none known, but an open resolver is abusable from outside and it ran for ~18 hours.

**What catches it next time:** a tool started for one task gets **stopped** afterwards, not left
running "just in case". And after starting anything that creates a virtual machine or containers,
check `lsof -nP -iTCP -sTCP:LISTEN` — what is newly listening, and on which interface.

---

## 2026-08-01 · Receipt acknowledgement would not have worked for the first 30 s on Linux

**What happened:** the acknowledgement cooldown used `0.0` as its default and compared it against
`time.monotonic()`. That counts from **system** start: on a long-running macOS box it is a large
number (the condition passes), on a freshly booted Linux it is close to zero (it does not). On
Ubuntu, no acknowledgement would have been sent for the first 30 seconds after start.

**Why it slipped through:** all 208 tests were green on macOS. The bug only appeared in a
container.

**Damage:** none — caught before deployment.

**What catches it next time:** run the tests on Linux **before** deploying, not after. And with
any time arithmetic, ask what it is actually counted from — `monotonic()` does not mean the same
thing on a machine up for a month and on a freshly started container.

---

## 2026-08-02 · A heart reaction sent the agent's internal note to the user

**What happened:** a ❤ reaction on a message produced the reply "No response requested." — an
internal note from the agent, not a message for the user.

**Mechanics:** the reaction branch of `_handle` injects "…no need to reply unless relevant." and
calls `_begin_turn()`, marking the turn as Telegram-originated. When the agent correctly stays
silent, the turn-end backstop reaches for the last assistant text and sends it. Two rules
contradicted each other outright: the reaction says "you need not reply", the backstop says "a
Telegram turn must not be left unanswered".

**Why it slipped through:** the backstop was built against lost replies and tested on ordinary
messages. A reaction is the one input that does **not** expect a reply — and nobody looked at it.

**Damage:** nothing substantive, only confusion. But it is a leak of internal text to a user,
which is precisely the class of bug that looks bad in front of an audience.

**What catches it next time:** for every new input channel, ask "does this input expect a reply?"
and check the backstop accordingly. The tests now contain `ReactionTurnBackstopTests`, which goes
through the real `_handle()`.

**A trap that was avoided:** the first draft of the fix set `_turn_text_sent = True`. That field
also drives the TUI bubbles and — worse — a reaction arriving **during** a running turn would
disarm the backstop for the real question underneath it. Hence a separate flag and the condition
"only when no turn was running".

---

## 2026-08-23 · Codex changed its log format — the bridge would have gone silent on the next update

**What happened:** a user reported that after updating to **Codex 0.149.0** the bridge stopped
delivering replies. The agent was visibly answering; nothing ever reached Telegram. The cause:
newer Codex writes the agent's reply only as `response_item/message` (role=assistant), while
`CodexReader` read exclusively `event_msg/agent_message`.

**Why it slipped through:** the reader was written against **one specific version** of a format
that belongs to a foreign tool and changes without notice. Our own Codex 0.144.4 writes both
forms, so nothing in our own operation could reveal it.

**Damage:** none here. For the reporter, hours of searching and an almost-unnecessary reinstall.
**It would have hit us on the first Codex update** — silencing the Codex bridge.

**What catches it next time:** the reader now reads both forms and dedups **by content**, not by
record type (`tests/test_readers_codex.py`, verified by mutation). The rule to remember: **a
foreign tool's log format is an API that changes without notice** — read tolerantly and keep a
test for both variants. A second trap found during the fix: an editable install can map the
package to a different checkout than the one you are editing, so a fix in the wrong copy never
reaches production. Check with
`python3 -c "import agent2telegram, os; print(os.path.dirname(agent2telegram.__file__))"`.

**The worst part is the shape of the failure:** the bridge does not crash and logs no error. It
simply goes quiet while still showing "typing". That is exactly the silent failure this log
exists to hunt.

---

## 2026-08-23 · Test messages were delivered to a real user

**What happened:** while comparing two checkouts, an **older copy** of
`tests/test_attach_backstop.py` was run against newer code. That copy does not set
`_use_durable_outbox = False`, so its fixture strings were written into the durable outbox in the
**shared** state directory — and the live bridge, a different process watching that same queue,
delivered them. Eleven messages of the form "the real answer", "thanks!", "a brand new answer".

**Why it slipped through:** the test's isolation rested on the author **remembering** to switch
the outbox off. New code added `_use_durable_outbox`; the old test knew nothing about it. Nothing
stood between the test and a real chat — and `b.tg` was a fake, so from the test's point of view
"nothing was sent". The real delivery took an entirely different route, through the disk.

**Damage:** eleven nonsense messages to a real person. Had the fixture contained anything
sensitive, it would have been worse.

**What catches it next time:** under pytest, `_ensure_outbox()` **refuses the default shared
queue**; a test has to declare its isolation explicitly (`_queue_path` or
`AGENT2TELEGRAM_STATE`). See `tests/test_outbox_isolation.py`, verified by mutation. The rule to
remember: **there must be no path from a test to a real user, even if the test author never
thinks about it** — opt-out is the wrong direction, the guarantee belongs in the code.


---

## 2026-08-23 · Subagent transcripts win the "newest" race

**What happened:** reported by a user running Claude Code with an architect/tester/reviewer setup:
the agent finishes its work, writes a summary, and nothing arrives. From outside it looks as if
the agent silently stopped reporting; you have to go and ask.

**Mechanics:** Claude Code stores subagent transcripts under `<conversation>/subagents/`.
`_newest_under()` globs recursively, and a running subagent writes far more often than the main
conversation — so on mtime it always wins. The bridge tails the subagent and the reply written to
the main conversation is never forwarded. In the reporter's directory, 17 of 21 transcripts were
subagents; in another installation, 413 of 426.

**Why it slipped through:** the resolver was written when subagents did not exist, and the
installations that would have shown it pin an explicit `transcript_path`, which skips the
resolver entirely. A bug in a code path you do not use yourself cannot be found by using the
software — only by someone whose setup differs.

**Damage:** none in the installations we can see; hours of confusion for the reporter.

**What catches it next time:** the resolver ignores anything under a `subagents/` path segment
(`tests/test_transcript_resolution.py`, verified by mutation). If only subagent transcripts exist
it returns nothing at all — better no transcript than the wrong one, because tailing the wrong
file forwards the wrong text. The wider rule: **the configuration you run yourself does not
exercise every path you ship.** A report from a differently configured user is evidence you
cannot generate on your own machine.

---

## 2026-08-23 · A finished message could be delivered twice

**What happened:** CI failed on a test that had passed locally: an inbound message was delivered
twice. `_inbound_done()` removed the record from the in-flight set BEFORE deleting it from the
durable inbox. In that window the record is "not in flight" and still "pending on disk", so a
concurrent `_replay_pending_inbound()` landing there submits it a second time.

**Why it slipped through:** the window is a few microseconds wide, and whether anything lands in
it depends on the machine. On a developer laptop the test passed; on a CI runner it did not. A
test that passes because of timing is not evidence — it is luck that has not run out yet.

**Damage:** none reaching a user. The queue exists precisely to make "exactly once" true, so this
was the failure mode it is built against, hiding inside the mechanism itself.

**What catches it next time:** the delete now happens first, so during cleanup the record still
counts as in flight and replay skips it; afterwards replay cannot see it at all. The in-flight id
is released in a `finally`, so a failed delete cannot strand it. The regression test does not
race — it runs the replay from INSIDE the delete, i.e. exactly in the window, so it fails
deterministically without the fix (`tests/test_v2_durability.py::InboundDoneOrderingTests`).

**The rule worth keeping:** when two data structures describe the same fact, the ORDER in which
they are updated is part of the contract, not an implementation detail. Write down which one may
briefly disagree, and make sure the disagreement is the safe direction. Here the safe direction
is "still busy" — a redundant skip costs nothing, a duplicate delivery costs trust.

---

## 2026-08-23 · A reply was lost, and nothing anywhere said so

**Not a near miss — this one reached the user.** A Telegram turn ended with no answer, no
backstop, and not a single line in the log. From the user's side the bridge simply went quiet;
from the log's side the turn looked completed and healthy.

**What happened:** during the turn the agent read an image. Claude Code files what a tool returns
under `type: "user"`, and the bridge decides from the last user text whether a turn came from
Telegram. The image record does not start with the origin prefix, so `_turn_from_tg` flipped to
False mid-turn. From that instant the turn counted as terminal-originated: the reply was not
forwarded, and the backstop — which only guards *Telegram* turns — never ran. Every guard was
working exactly as written; they were all reading a flag that had quietly become wrong.

**Why it slipped through:** the flag was re-derived from *every* user record instead of being
decided once by the message that started the turn. That is invisible until a turn happens to
contain a tool result, and it costs nothing until the one time it costs everything.

**Two layers, because one is not enough:**
1. The reader now recognises a tool result (`toolUseResult`, or a `tool_result` content block)
   and does not report it as user text.
2. A record without the origin prefix may no longer DOWNGRADE a running turn to local. Only the
   start of a new turn may reset the flag.

Layer 1 alone would not have fixed this case. Among the three records involved, two were proper
tool results — but the third was a plain-string user record (`[Image: 3300x1880 …]`) with nothing
in it to mark it as machine-generated. Only layer 2 catches that.

**Also added:** the end of every turn now logs `from_tg`, `text_sent` and `reaction`, and an
unanswered Telegram turn that somehow skips the backstop logs an error. The thing that made this
expensive was not the bug — it was that afterwards there was nothing to read.

**The rule worth keeping:** a flag that decides whether a user hears from you must be set by one
identifiable event, not recomputed from whatever passes by. And **silence must never be a valid
outcome**: if a turn ends unanswered, something has to say so.

---

## 2026-08-24 · The agent's own file-sending tool delivered nothing

**What happened:** the agent used its harness's built-in file-sending tool (Claude Code's
`SendUserFile`) three times in one day. Every time the text arrived and the file did not. No
error, no log line, nothing in the queue or dead-letter — the attachment simply ceased to exist.
The user noticed, twice, by asking "it didn't arrive".

**Why it slipped through:** the harness runs that tool ITSELF. The bridge never sees a call — the
only trace is a `tool_use` record in the transcript, which the bridge was reading and discarding
as an ordinary tool bubble. The bridge's own marker (`[tg-file]`) worked perfectly the whole time,
which made it look like an agent habit rather than a gap in the product. It is both: every Claude
Code user has that tool in their toolbox and will reach for it first, because it is the obvious
one.

**Damage:** three lost deliveries here, and a silent failure waiting for every Claude Code user
of the public project.

**What was rejected, and why it matters:** the first proposal was to detect intent from the reply
text ("sending", "attached", a filename) and warn when no marker was present. The maintainer
rejected it outright — it is language-dependent, guessy, and would fire false alarms. He was
right: the reply text is prose, and prose is the wrong place to look for a fact the transcript
already states exactly.

**The fix:** recognise the tool BY NAME in the transcript and deliver its files through the same
allowlist as a marker line. Deterministic, language-independent, and it works on the first try
without the agent knowing anything. The agent now reaches for the obvious tool and it simply
works.

**The rule worth keeping:** when an agent can do a thing two ways and only one of them is wired
up, that is not a documentation problem — the unwired path will be taken, because it looks
right. Wire it up, or make it fail loudly. Never let it be silent.

---

## 2026-08-29 · Two diagnoses in a row rested on a watchdog measuring something else

**What happened:** an operator watchdog reported `UNSENT INSTRUCTION in the window: "zapni
hlas", unchanged for 45 min` for a session driven by this bridge. On the strength of that
report a working bridge deployment was rolled back, and a follow-up note explained the same
report with an inbound FIFO queue "that failed to drain".

Neither held up:

* The FIFO queue was added in `06a2d59` and **reverted 47 minutes later** in `8665948` —
  it has not existed in this tree since June. The CLI's own mid-turn queue was measured live
  and drains within a second of the turn ending.
* The bridge journal for that hour contains the startup line and nothing else — no accepted
  update, and no `getUpdates` error either, although the poll loop logs both. All three
  bridges on the machine were equally silent. The message never reached the bot.
* The watchdog matched `^❯ <text>$` anywhere in the bottom rows of the pane — which is also
  the shape of the **echo of an already-answered message** in the transcript. Reproduced A/B
  on the same live pane in the same second: old rule "UNSENT INSTRUCTION", corrected rule
  silent. On the day itself it fired for three different sessions at the same minute, each
  with its own plausible text — three bots, three chats, one human, one second.

**Why it slipped through:** the watchdog's output *reads* like a measurement. Nobody asked
what it actually measures, and the explanation offered afterwards borrowed a commit from
the log without checking whether it was still in the tree.

**Damage:** none to the software — a working deployment was rolled back and about an hour
went into diagnosing a defect that was not there.

**What catches it next time:** in this bridge, no code path may drop an inbound message
without leaving a line in the log — the two silent `return`s in `_handle_update_once` and
the "handled but not delivered" cases of `_handle` now name themselves, at INFO, because
the deployed unit does not pass `-v` and a DEBUG line would leave the same nothing behind.
The watchdog itself was corrected to read its input line from the cursor position
(`#{cursor_y}`) instead of pattern-matching the bottom of the pane; it lives in the
operator's own repository, not here.

The checklist question for any monitoring output used as evidence: *what does this tool
observe, and what else looks the same to it?*

⚠️ **And a coda, because it is the same mistake one level down.** The first attempt at this
entry's fix had `_send_keys` verify that the Enter really submitted, by checking that the
tail of the injected text was gone from the cursor row. Review measured it on an 80-column
pane: an unsent message of 85–100 characters reported as *submitted*, because the tail fell
across the wrap and simply was not on that row. It would have deleted the durable record of
the very messages it was meant to protect, and its failure path re-typed the message up to
fifteen times. **It was removed, not patched.** A safeguard against a failure nobody has
reproduced, whose own failure modes are not measured either, is not a safeguard.
