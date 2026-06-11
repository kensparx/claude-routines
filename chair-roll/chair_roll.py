#!/usr/bin/env python3
"""
Discord Chair Roll
------------------
Each eligible roster member rolls 1d100; highest roll wins. Ties broken by random pick
among the tied. Posts the leaderboard + winner to Discord and updates state in the
config file.

PTO lookup is done by the parent Cowork/Claude session via the Google Calendar MCP.
Names of people on PTO for the target Monday are passed via --off.

Usage:
  # Dry-run (no Discord post, no config write):
  python3 chair_roll.py --target-date 2026-06-01 --off "" --dry-run

  # Real run:
  python3 chair_roll.py --target-date 2026-06-01 --off "Amber Clowe"

  # Connectivity test post:
  python3 chair_roll.py --test-post
"""

import argparse
import json
import os
import random
import sys
import urllib.request
import urllib.error
from datetime import datetime

CONFIG_PATH = os.environ.get(
    "CHAIR_ROLL_CONFIG",
    "/Users/kenyeung/Documents/Claude/Internal/.discord_bot_config",
)

# .env file for shared secrets (DISCORD_BOT_TOKEN, DISCORD_CHANNEL_ID, etc.)
# Resolution order: CHAIR_ROLL_ENV env var → sibling of config dir's parent → ~/Documents/Claude/.env
def _resolve_env_path():
    explicit = os.environ.get("CHAIR_ROLL_ENV")
    if explicit:
        return explicit
    # Search common locations for .env relative to the config file
    config_dir = os.path.dirname(os.path.abspath(CONFIG_PATH))
    candidates = [
        # Direct parent: .../Claude/Internal/../.env → .../Claude/.env
        os.path.join(config_dir, os.pardir, ".env"),
        # Sandbox: mounts are siblings under .../mnt/, so check .../mnt/Claude/.env
        os.path.join(config_dir, os.pardir, "Claude", ".env"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return os.path.abspath(c)
    return os.path.expanduser("~/Documents/Claude/.env")

ENV_PATH = _resolve_env_path()


def _load_env_file(path):
    """Read a .env file and return a dict of key=value pairs."""
    env = {}
    if not os.path.isfile(path):
        return env
    with open(path, "r") as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, _, v = s.partition("=")
            env[k.strip()] = v.strip()
    return env


def parse_config(path):
    cfg = {"roster": [], "_raw_lines": []}
    in_roster = False
    with open(path, "r") as f:
        for line in f:
            cfg["_raw_lines"].append(line)
            s = line.strip()
            if s == "ROSTER_BEGIN":
                in_roster = True
                continue
            if s == "ROSTER_END":
                in_roster = False
                continue
            if in_roster:
                if not s or s.startswith("#"):
                    continue
                parts = s.split(":")
                if len(parts) < 2:
                    continue
                cfg["roster"].append({
                    "name": parts[0].strip(),
                    "id": parts[1].strip(),
                    "pto_name": parts[2].strip() if len(parts) >= 3 else "",
                })
                continue
            if s.startswith("#") or not s or "=" not in s:
                continue
            k, _, v = s.partition("=")
            cfg[k.strip()] = v.strip()

    # Resolve secrets that aren't set directly in the config file.
    # Precedence for a blank config value: real process env var (cloud routine),
    # then a local .env file (local Mac run). This lets the same script work both
    # in a remote Claude routine (secrets injected as env vars) and locally.
    env = _load_env_file(ENV_PATH)
    for key in ("DISCORD_BOT_TOKEN", "DISCORD_CHANNEL_ID"):
        if cfg.get(key):
            continue
        cfg[key] = os.environ.get(key) or env.get(key, "")

    return cfg


def write_config_values(cfg, updates):
    out = []
    seen = set()
    for line in cfg["_raw_lines"]:
        ss = line.strip()
        wrote = False
        for k, v in updates.items():
            if ss.startswith(f"{k}=") and k not in seen:
                out.append(f"{k}={v}\n")
                seen.add(k)
                wrote = True
                break
        if not wrote:
            out.append(line)
    for k, v in updates.items():
        if k not in seen:
            if not out or not out[-1].endswith("\n"):
                out.append("\n")
            out.append(f"{k}={v}\n")
    with open(CONFIG_PATH, "w") as f:
        f.writelines(out)


def discord_post(token, channel_id, content, allowed_user_ids):
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    payload = {
        "content": content,
        "allowed_mentions": {
            "parse": [],
            "users": [str(u) for u in allowed_user_ids],
        },
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", f"Bot {token}")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "DiscordBot (https://sparxpg.com chair-roll, 1.0)")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def match_off_to_roster(off_pto_names, roster):
    matched = []
    for off in off_pto_names:
        off_l = off.strip().lower()
        if not off_l:
            continue
        hit = None
        for r in roster:
            if r["pto_name"]:
                if off_l.startswith(r["pto_name"].lower()):
                    hit = r
                    break
            else:
                first = off_l.split()[0] if off_l.split() else ""
                if first == r["name"].lower():
                    hit = r
                    break
        if hit and hit not in matched:
            matched.append(hit)
    return matched


def is_first_monday_of_month(date_str):
    """date_str = 'YYYY-MM-DD'. True iff this date is the first Monday of its month."""
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return False
    return d.weekday() == 0 and d.day <= 7


def find_roster_entry(roster, display_name):
    """Look up a roster entry by DisplayName (case-insensitive)."""
    target = (display_name or "").strip().lower()
    if not target:
        return None
    for r in roster:
        if r["name"].lower() == target:
            return r
    return None


def roll_for_everyone(eligible):
    """Each person rolls 1d100. Returns list of (person, roll) sorted desc by roll."""
    rolled = [(p, random.randint(1, 100)) for p in eligible]
    rolled.sort(key=lambda x: (-x[1], x[0]["name"].lower()))
    return rolled


def pick_winner(rolled):
    """Highest roll wins. Random pick among ties."""
    if not rolled:
        return None, 0, []
    top = rolled[0][1]
    tied = [p for p, r in rolled if r == top]
    winner = random.choice(tied) if len(tied) > 1 else tied[0]
    return winner, top, tied


def format_message(header, rolled, winner, winner_roll, tied, cycle_reset, remaining_after, off_roster, manual, target_date):
    lines = [header]
    lines.append("")
    for p, r in rolled:
        mark = " 👑" if p["name"] == winner["name"] else ""
        lines.append(f"{p['name']} 🎲 **{r}**{mark}" if mark else f"{p['name']} 🎲 {r}")
    lines.append("")
    if len(tied) > 1:
        tied_names = ", ".join(t["name"] for t in tied)
        lines.append(f"🪙 Tie at **{winner_roll}** between {tied_names} — coin-flip pick.")
    cycle_note = "Starting a new cycle. " if cycle_reset else ""
    lines.append(f"🏆 **This week's chair:** <@{winner['id']}> (rolled {winner_roll}). {cycle_note}")
    if remaining_after:
        names = ", ".join(p["name"] for p in remaining_after)
        lines.append(f"Remaining in cycle after this week: {names}.")
    else:
        lines.append("Everyone has now chaired this cycle — pool resets next week.")
    if off_roster:
        lines.append(f"On PTO that day: {', '.join(p['name'] for p in off_roster)}")
    if manual:
        lines.append(f"Manually excluded this week: {', '.join(manual)}")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--target-date", help="Target meeting Monday, YYYY-MM-DD")
    parser.add_argument("--off", default="", help="Comma-separated PTO full names off the target Monday")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--test-post", action="store_true")
    parser.add_argument("--test-roll", action="store_true", help="Full roll with [TEST] prefix, no pings, no state update.")
    parser.add_argument("--skip-reason", help="Post a skip-week message with this reason and exit. State not changed.")
    parser.add_argument("--seed", type=int, help="Deterministic seed (testing only).")
    args = parser.parse_args()

    if args.seed is not None:
        random.seed(args.seed)

    cfg = parse_config(CONFIG_PATH)
    token = cfg.get("DISCORD_BOT_TOKEN", "")
    channel = cfg.get("DISCORD_CHANNEL_ID", "")
    if not token or not channel:
        print("Missing DISCORD_BOT_TOKEN or DISCORD_CHANNEL_ID in config", file=sys.stderr)
        sys.exit(2)

    if args.test_post:
        status, body = discord_post(
            token, channel,
            "✅ Chair roll bot connected — test post. Real rolls will run Mondays at 11:45 AM PT.",
            [],
        )
        print(f"status={status}")
        if not (200 <= status < 300):
            print(body)
            sys.exit(1)
        sys.exit(0)

    if not args.target_date:
        print("--target-date is required (unless --test-post)", file=sys.stderr)
        sys.exit(2)

    # --- Skip-week logic ---
    # Either an explicit --skip-reason from the caller (e.g. today is a stat holiday),
    # or auto-detected: the target Monday is the first Monday of its month, which means
    # DEFAULT_FIRST_MONDAY_CHAIR auto-chairs and no roll is needed.
    skip_reason = args.skip_reason
    auto_first_monday = (not skip_reason) and is_first_monday_of_month(args.target_date)
    if auto_first_monday:
        default_chair_name = cfg.get("DEFAULT_FIRST_MONDAY_CHAIR", "").strip()
        if default_chair_name:
            skip_reason = (
                f"{args.target_date} is the first Monday of the month — "
                f"{default_chair_name} chairs by default"
            )
        else:
            skip_reason = (
                f"{args.target_date} is the first Monday of the month — no roll this week"
            )

    if skip_reason:
        ping_ids = []
        rendered_reason = skip_reason
        if auto_first_monday:
            default_chair_name = cfg.get("DEFAULT_FIRST_MONDAY_CHAIR", "").strip()
            chair_entry = find_roster_entry(cfg["roster"], default_chair_name)
            if chair_entry:
                rendered_reason = skip_reason.replace(
                    default_chair_name, f"<@{chair_entry['id']}>"
                )
                ping_ids = [chair_entry["id"]]

        # Don't double-period if the reason already ends in punctuation.
        tail = "" if rendered_reason.rstrip().endswith((".", "!", "?")) else "."
        msg = f"📅 **No roll this week.** {rendered_reason}{tail}"

        if args.dry_run:
            print("--- DRY RUN (skip) ---")
            print(f"Skip reason       : {skip_reason}")
            print(f"Auto first-Monday : {auto_first_monday}")
            print(f"Ping              : {ping_ids}")
            print("--- MESSAGE ---")
            print(msg)
            return

        if args.test_roll:
            msg = "🧪 **[TEST run — not a real skip notice. Nobody pinged.]**\n\n" + msg
            ping_ids = []

        status, body = discord_post(token, channel, msg, ping_ids)
        if not (200 <= status < 300):
            print(f"Discord post failed: status={status} body={body}", file=sys.stderr)
            sys.exit(4)
        print(f"SKIP OK. Reason: {skip_reason}")
        return

    roster = cfg["roster"]
    chaired = [n.strip() for n in cfg.get("CHAIRED_THIS_CYCLE", "").split(",") if n.strip()]
    manual = [n.strip() for n in cfg.get("MANUAL_EXCLUSIONS", "").split(",") if n.strip()]
    off_pto_names = [n.strip() for n in args.off.split(",") if n.strip()]
    off_roster = match_off_to_roster(off_pto_names, roster)
    off_names = [r["name"] for r in off_roster]

    excluded = set(chaired) | set(manual) | set(off_names)
    eligible = [r for r in roster if r["name"] not in excluded]

    cycle_reset = False
    if not eligible:
        cycle_reset = True
        excluded = set(manual) | set(off_names)
        chaired = []
        eligible = [r for r in roster if r["name"] not in excluded]

    if not eligible:
        print("No eligible candidates even after cycle reset. Aborting.", file=sys.stderr)
        sys.exit(3)

    rolled = roll_for_everyone(eligible)
    winner, top_roll, tied = pick_winner(rolled)

    new_chaired = ([winner["name"]] if cycle_reset else chaired + [winner["name"]])
    remaining_after = [
        r for r in roster
        if r["name"] not in (set(new_chaired) | set(manual) | set(off_names))
    ]

    header = cfg.get(
        "MESSAGE_HEADER",
        "🎲 **Chair roll for the {date} meeting.** Highest d100 wins.",
    ).format(date=args.target_date)

    msg = format_message(
        header=header,
        rolled=rolled,
        winner=winner,
        winner_roll=top_roll,
        tied=tied,
        cycle_reset=cycle_reset,
        remaining_after=remaining_after,
        off_roster=off_roster,
        manual=manual,
        target_date=args.target_date,
    )

    if args.dry_run:
        print("--- DRY RUN ---")
        print(f"Target date     : {args.target_date}")
        print(f"PTO names in    : {off_pto_names}")
        print(f"PTO matched     : {[r['name'] for r in off_roster]}")
        print(f"Manual exclude  : {manual}")
        print(f"Chaired so far  : {chaired}")
        print(f"Eligible pool   : {[r['name'] for r in eligible]}")
        print(f"Rolls (desc)    : {[(p['name'], r) for p, r in rolled]}")
        print(f"Top roll        : {top_roll}")
        print(f"Tied at top     : {[t['name'] for t in tied]}")
        print(f"Winner          : {winner['name']}")
        print(f"Cycle reset     : {cycle_reset}")
        print("--- MESSAGE ---")
        print(msg)
        return

    if args.test_roll:
        test_msg = "🧪 **[TEST run — not a real chair pick. No state changed, nobody pinged.]**\n\n" + msg
        status, body = discord_post(token, channel, test_msg, [])
        if not (200 <= status < 300):
            print(f"Discord post failed: status={status} body={body}", file=sys.stderr)
            sys.exit(4)
        print(f"TEST OK. Would-be winner={winner['name']} roll={top_roll} target={args.target_date}")
        return

    status, body = discord_post(token, channel, msg, [winner["id"]])
    if not (200 <= status < 300):
        print(f"Discord post failed: status={status} body={body}", file=sys.stderr)
        sys.exit(4)

    write_config_values(cfg, {
        "CHAIRED_THIS_CYCLE": ",".join(new_chaired),
        "MANUAL_EXCLUSIONS": "",
    })

    print(f"OK. Winner={winner['name']} roll={top_roll} target={args.target_date} cycle_reset={cycle_reset}")


if __name__ == "__main__":
    main()
