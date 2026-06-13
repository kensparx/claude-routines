#!/usr/bin/env python3
"""
CSE photo routine — engine (deterministic part).

Reads the Zapier photo sheet for un-filed photos, matches each to an event (via
the Marketing Activity Tracker's Industry Events tab), ensures the event's Drive
folder structure exists, files the photo into `event photos/`, and writes the
event + folder back to the photo sheet. Emits a JSON manifest of filed photos
for the cropping step (done by the routine/agent with vision).

Auth: service account. Local → key file; cloud → GOOGLE_SA_KEY env (JSON string).

SAFE BY DEFAULT: --dry-run (no writes) unless --apply is passed.
High-confidence matches (one event whose date range covers the photo date) are
auto-filed; ambiguous / no-match photos are left untouched and reported.

Usage:
  python3 cse_photo_routine.py            # dry-run: report matches, no writes
  python3 cse_photo_routine.py --apply    # actually file + write back
"""
import os, sys, json, re, io, datetime
import urllib.request, urllib.parse, urllib.error
from google.oauth2 import service_account
import google.auth.transport.requests as gtr

SCOPES = ["https://www.googleapis.com/auth/drive", "https://www.googleapis.com/auth/spreadsheets"]
PHOTO_SHEET = "1yfgmeRyBw6_3tXktxAMkupiwEnfRTT0N66Pw4e-12qE"
TRACKER = "1nub-P9a5Eo-CQPuzfuWnQ1N4Nj7GU2dLgz3CRSKkBO4"
EVENTS_ROOT = "1ZeshoP3i5dD_ZEUfuw5e5ch4CP56HcZX"   # "2026 Events" folder in the Shared Drive
SHARED_DRIVE = "0AND05yAtslbrUk9PVA"
KEY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "alpine-ship-488900-i2-c4b1e59386c7.json")
APPLY = "--apply" in sys.argv

# ---------- auth ----------
def credentials():
    raw = os.environ.get("GOOGLE_SA_KEY")
    if raw:
        raw = raw.strip()
        if not raw.startswith("{"):           # accept base64-encoded JSON too
            import base64
            raw = base64.b64decode(raw).decode()
        return service_account.Credentials.from_service_account_info(json.loads(raw), scopes=SCOPES)
    return service_account.Credentials.from_service_account_file(KEY_FILE, scopes=SCOPES)

CREDS = credentials(); CREDS.refresh(gtr.Request())

def api(method, url, body=None, raw=False):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
        headers={"Authorization": "Bearer " + CREDS.token,
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return r.read() if raw else json.loads(r.read())

# ---------- sheets ----------
def sheet_get(sid, rng):
    return api("GET", f"https://sheets.googleapis.com/v4/spreadsheets/{sid}/values/{urllib.parse.quote(rng)}").get("values", [])

def sheet_update(sid, rng, values):
    api("PUT", f"https://sheets.googleapis.com/v4/spreadsheets/{sid}/values/{urllib.parse.quote(rng)}?valueInputOption=USER_ENTERED",
        {"values": values})

# ---------- date parsing ----------
def parse_event_date(s):
    s = (s or "").strip()
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%Y-%m-%d", "%m/%d/%Y"):
        try: return datetime.datetime.strptime(s, fmt).date()
        except ValueError: continue
    return None

def parse_photo_date(s):
    s = (s or "").strip()
    try: return datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except Exception: return None

# ---------- drive ----------
def drive_list(q, fields="files(id,name)"):
    url = ("https://www.googleapis.com/drive/v3/files?" + urllib.parse.urlencode({
        "q": q, "fields": fields, "pageSize": 200,
        "corpora": "drive", "driveId": SHARED_DRIVE,
        "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}))
    return api("GET", url).get("files", [])

def folder_id_from_link(link):
    if not link: return None
    m = re.search(r"/folders/([A-Za-z0-9_-]+)", link) or re.search(r"[?&]id=([A-Za-z0-9_-]+)", link)
    return m.group(1) if m else None

def find_or_make_subfolder(parent, name, dry):
    # case-insensitive reuse so existing folders ("edits for LinkedIn" vs "edits for linkedin") aren't duplicated
    kids = drive_list(f"'{parent}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false")
    for k in kids:
        if k["name"].strip().lower() == name.strip().lower():
            return k["id"]
    if dry: return f"(would-create:{name})"
    f = api("POST", "https://www.googleapis.com/drive/v3/files?supportsAllDrives=true",
            {"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent]})
    return f["id"]

def file_id_from_gdlink(link):
    m = re.search(r"[?&]id=([A-Za-z0-9_-]+)", link or "") or re.search(r"/d/([A-Za-z0-9_-]+)", link or "")
    return m.group(1) if m else None

# ---------- event matching ----------
def norm(s): return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()

def load_events():
    rows = sheet_get(TRACKER, "Industry Events!A2:M")
    events = []
    for i, r in enumerate(rows):
        r = r + [""] * (13 - len(r))
        start = parse_event_date(r[1]); 
        if not start: continue           # skip section headers / undated rows
        end = parse_event_date(r[2]) or start
        events.append({"name": r[0].strip(), "start": start, "end": end,
                       "gd": r[12].strip(), "row": i + 2,
                       "attendees": (r[4] + " " + r[5]).strip()})   # lead + other attendees
    return events

def load_roster():
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "roster.json")
    try:
        d = json.load(open(p))
        return [{"canonical": x["canonical"], "aliases": [a.lower() for a in x.get("aliases", [])]}
                for x in d.get("people", [])]
    except Exception:
        return []

def attendee_hits(ev, photo_text, roster):
    """Canonical people who BOTH attend ev (tracker attendees) AND are named in the photo text."""
    att, txt = norm(ev.get("attendees", "")), norm(photo_text)
    return {p["canonical"] for p in roster
            if any(a in att for a in p["aliases"]) and any(a in txt for a in p["aliases"])}

def match(photo_date, comments, user, events, roster):
    photo_text = f"{comments} {user}"
    covering = [e for e in events if e["start"] <= photo_date <= e["end"]]
    if len(covering) == 1: return covering[0], "date"
    if len(covering) > 1:
        named = [e for e in covering if attendee_hits(e, photo_text, roster)]   # attendee in the photo
        if len(named) == 1: return named[0], "attendee-match"
        c = norm(comments)                                                       # else event name in comment
        byname = [e for e in covering if e["name"] and norm(e["name"]).split()[0] in c]
        if len(byname) == 1: return byname[0], "name-in-comment"
        return None, f"ambiguous ({len(covering)} events cover {photo_date})"
    near = [e for e in events if abs((e["start"] - photo_date).days) <= 1 or abs((e["end"] - photo_date).days) <= 1]
    if len(near) == 1: return near[0], "near(±1d)"
    return None, f"no event covers {photo_date}"

def resolve_folder(ev, dry):
    fid = folder_id_from_link(ev["gd"])
    if fid: return fid, "tracker-M", None
    # search Shared Drive event folders by date prefix or name
    datepfx = ev["start"].strftime("%Y-%m-%d")
    cands = drive_list(f"'{EVENTS_ROOT}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false and name contains '{datepfx}'")
    if not cands and ev["name"]:
        tok = ev["name"].split()[0]
        cands = [c for c in drive_list(f"'{EVENTS_ROOT}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false") if norm(tok) in norm(c["name"])]
    if cands:
        return cands[0]["id"], f"matched-folder '{cands[0]['name']}'", cands[0]["id"]
    newname = f"{datepfx} {ev['name']}".strip()
    if dry: return f"(would-create:{newname})", "create", None
    f = api("POST", "https://www.googleapis.com/drive/v3/files?supportsAllDrives=true&fields=id,webViewLink",
            {"name": newname, "mimeType": "application/vnd.google-apps.folder", "parents": [EVENTS_ROOT]})
    return f["id"], f"created '{newname}'", f["id"]

# ---------- main ----------
def main():
    dry = not APPLY
    print(f"=== CSE photo routine ({'DRY-RUN' if dry else 'APPLY'}) ===")
    try:
        from zoneinfo import ZoneInfo
        van = datetime.datetime.now(ZoneInfo("America/Vancouver")).strftime("%Y-%m-%d")
    except Exception:
        van = datetime.date.today().isoformat()
    print("VANCOUVER_DATE:", van)
    events = load_events()
    roster = load_roster()
    print(f"loaded {len(events)} dated events from tracker, {len(roster)} roster people")
    rows = sheet_get(PHOTO_SHEET, "Sheet1!A2:F")
    manifest, filed, ambiguous = [], 0, 0
    for i, r in enumerate(rows):
        r = r + [""] * (6 - len(r))
        rownum = i + 2
        date_s, user, comments, gdlink, assoc, gdfolder = r[:6]
        if assoc.strip() or not gdlink.strip():   # already filed, or no photo
            continue
        pdate = parse_photo_date(date_s)
        if not pdate:
            print(f"  row{rownum}: unparseable date '{date_s}' — skip"); continue
        ev, how = match(pdate, comments, user, events, roster)
        if not ev:
            ambiguous += 1
            print(f"  row{rownum} [{pdate}] {comments[:30]!r}: NO MATCH — {how}")
            continue
        folder, src, backfill = resolve_folder(ev, dry)
        # structure is Event -> event photos -> edits for LinkedIn (edits nested INSIDE event photos)
        ep = find_or_make_subfolder(folder, "event photos", dry) if not str(folder).startswith("(") else "(pending)"
        ed = find_or_make_subfolder(ep, "edits for LinkedIn", dry) if not str(ep).startswith("(") else "(pending)"
        print(f"  row{rownum} [{pdate}] -> '{ev['name']}' ({how}; folder {src})")
        pf = file_id_from_gdlink(gdlink)
        local = ""
        entry = {"row": rownum, "event": ev["name"], "photo_file": pf,
                 "event_photos_folder": ep, "edits_folder": ed, "local": ""}
        filed += 1
        if not dry:
            # copy photo into event photos
            if pf and not str(ep).startswith("("):
                try: api("POST", f"https://www.googleapis.com/drive/v3/files/{pf}/copy?supportsAllDrives=true",
                         {"parents": [ep]})
                except urllib.error.HTTPError as e: print(f"     copy failed: {e.code}")
            # download original locally so the cropping step (vision) can view it
            if pf:
                os.makedirs("_filed", exist_ok=True)
                local = os.path.abspath(f"_filed/{rownum}.jpg")
                try:
                    b = api("GET", f"https://www.googleapis.com/drive/v3/files/{pf}?alt=media&supportsAllDrives=true", raw=True)
                    open(local, "wb").write(b); entry["local"] = local
                except urllib.error.HTTPError as e: print(f"     download failed: {e.code}")
            # folder webViewLink + write-back to photo sheet (E/F) + backfill tracker M
            link = api("GET", f"https://www.googleapis.com/drive/v3/files/{folder}?fields=webViewLink&supportsAllDrives=true").get("webViewLink", "")
            sheet_update(PHOTO_SHEET, f"Sheet1!E{rownum}:F{rownum}", [[ev["name"], link]])
            if backfill and not folder_id_from_link(ev["gd"]):
                sheet_update(TRACKER, f"Industry Events!M{ev['row']}",
                             [[f"https://drive.google.com/drive/folders/{backfill}"]])
        manifest.append(entry)
    print(f"\nSUMMARY: {filed} photo(s) {'would be ' if dry else ''}filed, {ambiguous} need human review")
    print("MANIFEST:", json.dumps(manifest))

if __name__ == "__main__":
    main()
