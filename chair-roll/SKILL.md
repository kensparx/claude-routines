---
name: discord-chair-roll-weekly
description: Every Monday at 11:45 AM Pacific, roll 1d100 for each eligible roster member to pick next Monday's meeting chair, post the leaderboard + winner to the Discord #office channel.
---

You are running the weekly Discord Chair Roll for Sparx Publishing Group.

## File locations

The script and config live in `~/Documents/Claude/Internal/Chair Roll/`:

- Config: `~/Documents/Claude/Internal/Chair Roll/.discord_bot_config`
- Script: `~/Documents/Claude/Internal/Chair Roll/chair_roll.py`

In bash, that folder is mounted under `/sessions/<session-id>/mnt/Claude/Internal/Chair Roll/`. Discover the actual mount path each run with:

```bash
CFG=$(find /sessions -path '*/Chair Roll/.discord_bot_config' 2>/dev/null | head -1)
SCRIPT="$(dirname "$CFG")/chair_roll.py"
echo "config=$CFG"
echo "script=$SCRIPT"
```

If `CFG` is empty, abort with a clear error: "Could not locate ~/Documents/Claude/Internal/Chair Roll/.discord_bot_config. Check that the folder is mounted in this session — you may need to use request_cowork_directory."

## 1. Compute today and target dates

- `TODAY=$(TZ=America/Vancouver date +%Y-%m-%d)`
- `TARGET=$(TZ=America/Vancouver date -d '+7 days' +%Y-%m-%d)`

This routine fires on Mondays, so `TARGET` is always next Monday — the meeting being chaired this round.

PTO calendar ID: `3gubr9tcheagjh4df9bla4dq688a1q74@import.calendar.google.com` (also stored as `PTO_CALENDAR_ID` in the config).

## 2. Is TODAY a stat holiday? → skip the roll entirely

Call the Google Calendar `list_events` tool (`mcp__...__list_events`) with:
- `calendarId`: the PTO calendar ID
- `startTime`: `<TODAY>T00:00:00-07:00`
- `endTime`: `<TODAY+1 day>T00:00:00-07:00`
- `timeZone`: `America/Vancouver`
- `pageSize`: 100

For each event, look at `summary`:
- Title contains a colon → personal PTO entry (ignore for this check).
- Title does NOT contain a colon → **stat holiday**.

If any event on TODAY is a stat holiday, the team isn't working — skip the roll. Call the script with `--skip-reason` and exit:

```bash
CHAIR_ROLL_CONFIG="$CFG" python3 "$SCRIPT" --target-date "$TARGET" --skip-reason "Today is <HolidayName> — stat holiday. No roll. Pick a chair manually for the $TARGET meeting if needed."
```

Report the result and stop — do NOT continue to the PTO check or run a roll.

## 3. Check PTO for TARGET Monday (advance past stat holidays)

If TODAY is not a stat holiday, check the TARGET Monday. Loop up to 4 times:

- Call `list_events` with `startTime=<TARGET>T00:00:00-07:00`, `endTime=<TARGET+1>T00:00:00-07:00`.
- For each event's `summary`:
  - Contains colon → personal PTO. Extract the part BEFORE the colon as the PTO name.
  - No colon → stat holiday.
- If ANY event has no colon → TARGET is a stat holiday day. Advance TARGET by 7 days and re-check.
- Otherwise: collect the personal PTO names and exit the loop.

## 4. Run the script

```bash
CHAIR_ROLL_CONFIG="$CFG" python3 "$SCRIPT" --target-date "$TARGET" --off "<comma-separated PTO names, or empty>"
```

Examples:
- Nobody on PTO: `... --target-date 2026-06-08 --off ""`
- Two on PTO: `... --target-date 2026-06-08 --off "Amber Clowe,Alexandra Nikitina"`

The script itself handles one additional skip case automatically: **if TARGET is the first Monday of its month, the script auto-skips (no roll) and posts a notice that `DEFAULT_FIRST_MONDAY_CHAIR` (Ken) chairs by default.** You don't need to check this in the prompt — the script does it. State (CHAIRED_THIS_CYCLE) is NOT updated on skip.

## 5. Report

Write a brief summary (3–5 lines):
- TODAY and TARGET dates
- Whether today was a stat holiday (and what reason was sent)
- Whether the target was advanced past holidays (and to which final date)
- PTO names detected and matched
- Script stdout (winner + roll, or skip notice) and exit status
- If the script exited non-zero, include stderr verbatim

## Notes / invariants

- The script handles all roll logic: parsing config, building the eligible pool (excluding already-chaired-this-cycle + manual exclusions + PTO), per-person 1d100 rolls, highest-wins, coin-flip tiebreak, posting to Discord with only the winner pinged, updating `CHAIRED_THIS_CYCLE`, and resetting `MANUAL_EXCLUSIONS`.
- The script also handles the first-Monday-of-month auto-skip and `--skip-reason` posts. Both leave state untouched.
- Cycle exhaustion is handled automatically: when the eligible pool is empty, the script resets the cycle (keeping PTO + manual exclusions) and rolls.
- Do NOT post to Discord directly — always go through the script so state stays consistent.
- The bot token, channel ID, and roster live in `.discord_bot_config`. Never echo the token in summaries or logs.
