You are running the weekly Discord Chair Roll for Sparx Publishing Group as a remote Claude routine.

This routine runs in a shared environment (also used by the Birthday and Time Off routines). It is NOT bound to a repo, so it clones the `kensparx/claude-routines` repo at runtime, rolls, and pushes the updated state back so the rotation persists between weeks.

## Environment variables (injected by the environment)

- `DISCORD_BOT_TOKEN`, `DISCORD_CHANNEL_ID` — read automatically by the script.
- `GITHUB_KENSPARX_TOKEN` — used to clone and push the repo. Never echo any of these.

Sanity-check first:

```bash
echo "discord_token=$([ -n "$DISCORD_BOT_TOKEN" ] && echo yes || echo NO)"
echo "discord_channel=$([ -n "$DISCORD_CHANNEL_ID" ] && echo yes || echo NO)"
echo "github_token=$([ -n "$GITHUB_KENSPARX_TOKEN" ] && echo yes || echo NO)"
```

If `DISCORD_BOT_TOKEN` or `DISCORD_CHANNEL_ID` is missing, do NOT post — report the block and stop. If `GITHUB_KENSPARX_TOKEN` is missing, you can still roll and post, but state cannot be saved — call this out loudly in the report.

## 1. Clone the repo

```bash
WORK=$(mktemp -d)
git clone --depth 1 "https://x-access-token:${GITHUB_KENSPARX_TOKEN}@github.com/kensparx/claude-routines.git" "$WORK" 2>&1 | grep -v -i token
cd "$WORK"
ls chair-roll/chair_roll.py chair-roll/config || { echo "Repo files missing — aborting."; exit 1; }
```

All subsequent commands run from `$WORK`. The script is `chair-roll/chair_roll.py`, the config + state is `chair-roll/config`.

## 2. Compute today and target dates

```bash
TODAY=$(TZ=America/Vancouver date +%Y-%m-%d)
TARGET=$(TZ=America/Vancouver date -d '+7 days' +%Y-%m-%d)
```

This routine fires on Mondays, so `TARGET` is always next Monday — the meeting being chaired this round.

PTO calendar ID: `3gubr9tcheagjh4df9bla4dq688a1q74@import.calendar.google.com`

## 3. Is TODAY a stat holiday? → skip the roll entirely

Call the Google Calendar `list_events` tool with:
- `calendarId`: the PTO calendar ID
- `startTime`: `<TODAY>T00:00:00-07:00`
- `endTime`: `<TODAY+1 day>T00:00:00-07:00`
- `timeZone`: `America/Vancouver`
- `pageSize`: 100

For each event, look at `summary`:
- Title contains a colon → personal PTO entry (ignore for this check).
- Title does NOT contain a colon → **stat holiday**.

If any event on TODAY is a stat holiday, the team isn't working — skip the roll:

```bash
CHAIR_ROLL_CONFIG=chair-roll/config python3 chair-roll/chair_roll.py \
  --target-date "$TARGET" \
  --skip-reason "Today is <HolidayName> — stat holiday. No roll. Pick a chair manually for the $TARGET meeting if needed."
```

A skip does not change state, so there is nothing to commit. Report and stop — do NOT continue to the PTO check or run a roll.

## 4. Check PTO for TARGET Monday (advance past stat holidays)

If TODAY is not a stat holiday, check the TARGET Monday. Loop up to 4 times:

- Call `list_events` with `startTime=<TARGET>T00:00:00-07:00`, `endTime=<TARGET+1>T00:00:00-07:00`.
- For each event's `summary`:
  - Contains colon → personal PTO. Extract the part BEFORE the colon as the PTO name.
  - No colon → stat holiday.
- If ANY event has no colon → TARGET is a stat holiday day. Advance TARGET by 7 days (`TZ=America/Vancouver date -d "$TARGET +7 days" +%Y-%m-%d`) and re-check.
- Otherwise: collect the personal PTO names and exit the loop.

## 5. Run the roll

```bash
CHAIR_ROLL_CONFIG=chair-roll/config python3 chair-roll/chair_roll.py \
  --target-date "$TARGET" --off "<comma-separated PTO names, or empty>"
```

Examples:
- Nobody on PTO: `--target-date 2026-06-15 --off ""`
- Two on PTO: `--target-date 2026-06-15 --off "Amber Clowe,Alexandra Nikitina"`

The script auto-skips the first Monday of the month (Ken chairs by default) and exits without changing state.

## 6. Commit state back to the repo

A real roll updates `CHAIRED_THIS_CYCLE` and clears `MANUAL_EXCLUSIONS` in `chair-roll/config`. Persist that — otherwise the rotation resets every week:

```bash
git config user.email "routines@sparxpg.com"
git config user.name  "Chair Roll Bot"
if ! git diff --quiet chair-roll/config; then
  git add chair-roll/config
  git commit -m "chair-roll: update cycle state after $TARGET roll"
  git push 2>&1 | grep -v -i token
  echo "State committed."
else
  echo "No state change to commit (skip week)."
fi
```

## 7. Report

Write a brief summary (3–5 lines):
- TODAY and TARGET dates
- Whether today was a stat holiday (and what reason was sent)
- Whether the target was advanced past holidays (and to which final date)
- PTO names detected and matched
- Script stdout (winner + roll, or skip notice) and exit status
- Whether state was committed
- If the script exited non-zero, include stderr verbatim

## Notes / invariants

- The script handles all roll logic: eligible pool, 1d100 rolls, highest-wins, coin-flip tiebreak, Discord post, state update.
- Do NOT post to Discord directly — always go through the script so state stays consistent.
- Never echo `DISCORD_BOT_TOKEN` or `GITHUB_KENSPARX_TOKEN` in summaries or logs.
- The Discord bot is shared with other Sparx routines — do not change its token or channel.
