# Deployment checklist

Work from the list, not from memory — even the tenth time. Tick one box at a time. **If any item
fails, go straight to ROLLBACK**; do not improvise the rest.

This is the checklist for putting a new version of the bridge in front of real users. It exists
because every line in [NEAR-MISSES.md](NEAR-MISSES.md) was written by somebody who was sure they
remembered the steps.

## Before the switch

- [ ] `git status` is clean, everything committed
- [ ] the full test battery is green on your own machine
- [ ] the full test battery is green on Linux, on **two** Python versions (e.g. 3.12 and 3.10)
- [ ] mutation check passes — every fix has a test that goes red when the fix is reverted
- [ ] the currently deployed version is untouched and can be returned to
- [ ] if you run several bridges, leave at least one on the old version as a fallback channel
- [ ] write down the time of the switch, so the logs can be read against it afterwards

## The switch

⚠️ **`ps` CANNOT TELL two versions apart** — both have identical argv and both log
`Attach bridge live`. The only reliable distinction is the process's working directory.

- [ ] record the PID of the running bridge: `ps -axww -o pid=,args= | grep "config.json"`
- [ ] verify its working directory: `lsof -a -p <PID> -d cwd`
- [ ] point the launcher at the new checkout
- [ ] `bash -n <launcher>.sh` (syntax)
- [ ] stop the bridge **by PID**, never by pattern
- [ ] wait for the supervisor to bring it back (within a minute)
- [ ] **verify the new process's working directory** — only that proves the new version is running
- [ ] exactly **one** instance is running
- [ ] the update offset continues where it left off (it did not reset to zero) — otherwise the
      whole backlog replays

## Verification in production, not in theory

- [ ] send yourself a message from Telegram → **a reply arrives**
- [ ] send a long reply (over 4000 characters) → **it does not arrive twice**
- [ ] send an attachment → **it arrives**
- [ ] send a voice note → **it is transcribed**
- [ ] `agent2telegram notify "test"` → **it arrives** (the path used by background jobs)
- [ ] `python3 tools/daily_report.py --max-lines 200` → runs and reports no losses

## ROLLBACK (must be rehearsed BEFORE it is needed)

⚠️ **Rollback is not free.** An older version does not read newer queues. Going back with a
message still inside means it is stranded: Telegram will not send it again and the old version
does not know it exists.

- [ ] **the queues are empty**: `ls <state>/inbox/*.json <state>/queue/outbox/*.json` → nothing
- [ ] no turn is in flight (no open `TURN START` without a `TURN END` in the log)
- [ ] only then point the launcher back at the previous checkout
- [ ] stop the bridge → the supervisor brings it back from the old path
- [ ] verify it runs from the old path and answers a message

**A rollback nobody has tried is not a rollback.** Rehearse it as part of the deployment, not at
the moment of trouble.

## Weekly operation

- [ ] run the daily report and deliver it through `agent2telegram notify` (a cron turn has no
      path back to the chat on its own)
- [ ] write every finding into [NEAR-MISSES.md](NEAR-MISSES.md), including the answer to
      "what catches this next time, by itself?"
- [ ] after a week: decide whether the old queue can be removed — the condition is that nothing
      ever had to fall back to it
