#!/usr/bin/env python3
"""
Watch Better's OpenActive feeds for Rivermead badminton (40 min) court slots
and send a phone alert the moment a wanted slot has courts free.

Usage:
  python better_watch.py discover   # once: find Rivermead badminton-40min court IDs
  python better_watch.py watch      # one pass (use from cron every ~10-15 min)
  python better_watch.py burst      # poll every 30s for ~8 min (cron at 21:58)

Alerts: set NTFY_TOPIC (install the free ntfy app, subscribe to that topic).
Python 3.9+, standard library only. Does NOT log in or book anything.
Data licence: CC-BY 4.0, attribution "Better".
"""
import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = "https://better-admin.org.uk/api/openactive/better"
VENUE_SLUG = "rivermead-leisure-complex"
ACTIVITY_SLUG = "badminton-40min"

# weekday (Mon=0) -> list of (earliest start, latest end), UK local time.
# A slot is wanted if it lies fully inside one of these windows.
EVENING = [("20:00", "22:00")]
WINDOWS = {0: EVENING, 1: EVENING, 2: EVENING, 3: EVENING}  # Mon-Thu
# To also watch the morning slots, e.g.:
# WINDOWS[0] = EVENING + [("08:40", "09:20")]

LONDON = ZoneInfo("Europe/London")
STATE_FILE = Path(os.environ.get("BETTER_STATE", "better_state.json"))
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
UA = {"User-Agent": "personal-court-watcher/1.0 (read-only OpenActive consumer)"}


def get_json(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def crawl(url):
    """Follow RPDE 'next' links; yield (items, next_url) per page until caught up."""
    while True:
        page = get_json(url)
        items = page.get("items", [])
        nxt = page.get("next") or url
        yield items, nxt
        if not items or nxt == url:
            return
        url = nxt


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"courts": {}, "cursor": None, "slots": {}, "notified": []}


def save_state(st):
    STATE_FILE.write_text(json.dumps(st))


def discover(st):
    """Crawl facility-uses (slow, run once) and keep Rivermead badminton-40min."""
    courts = {}
    pages = 0
    for items, _ in crawl(f"{BASE}/facility-uses"):
        pages += 1
        for it in items:
            d = it.get("data") or {}
            url = d.get("url", "")
            if f"/location/{VENUE_SLUG}/{ACTIVITY_SLUG}" not in url:
                continue
            for ind in d.get("individualFacilityUse", []):
                courts[ind["@id"]] = {
                    "name": ind.get("name", d.get("name", "")),
                    "url": ind.get("url", url),
                }
        print(f"  scanned page {pages}, found {len(courts)} so far", flush=True)
    st["courts"] = courts
    st["discovered"] = True
    save_state(st)
    print(json.dumps(courts, indent=2))
    if not courts:
        print("No courts found. Check VENUE_SLUG / ACTIVITY_SLUG.")


def local(iso):
    return datetime.fromisoformat(iso).astimezone(LONDON)


def wanted(start_iso, end_iso):
    s, e = local(start_iso), local(end_iso)
    if s.date() != e.date():
        return False
    for lo, hi in WINDOWS.get(s.weekday(), []):
        if lo <= s.strftime("%H:%M") and e.strftime("%H:%M") <= hi:
            return True
    return False


def notify(title, body, click):
    print(f"ALERT: {title} | {body} | {click}", flush=True)
    if not NTFY_TOPIC:
        return
    req = urllib.request.Request(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=body.encode(),
        headers={"Title": title, "Click": click, "Priority": "high", **UA},
    )
    try:
        urllib.request.urlopen(req, timeout=15)
    except Exception as exc:  # alert failure must not kill the watcher
        print("ntfy failed:", exc)


def start_cursor():
    # RPDE feeds are ordered by modification time, oldest first. Start a few
    # days back instead of crawling years of history. Falls back to full crawl.
    return f"{BASE}/slots?afterTimestamp={int(time.time()) - 3 * 86400}&afterId="


def watch_once(st):
    if not st.get("discovered"):
        discover(st)  # first run only (slow); result is kept in the state file
    if not st["courts"]:
        sys.exit("No Rivermead badminton-40min courts found; check the slugs.")
    cursor = st["cursor"] or start_cursor()
    try:
        pages = list(crawl(cursor))
    except urllib.error.HTTPError:
        pages = list(crawl(f"{BASE}/slots"))  # fallback: full history (slow)
    for items, nxt in pages:
        for it in items:
            sid = it["id"]
            if it.get("state") == "deleted" or not it.get("data"):
                st["slots"].pop(sid, None)
                continue
            d = it["data"]
            if d.get("facilityUse") not in st["courts"]:
                continue
            st["slots"][sid] = {
                "start": d["startDate"],
                "end": d["endDate"],
                "left": d.get("remainingUses", 0),
                "max": d.get("maximumUses"),
                "fu": d["facilityUse"],
            }
        st["cursor"] = nxt

    now = time.time()
    fresh = []
    for sid, s in sorted(st["slots"].items(), key=lambda kv: kv[1]["start"]):
        if local(s["end"]).timestamp() < now:  # slot is in the past
            del st["slots"][sid]
            if sid in st["notified"]:
                st["notified"].remove(sid)
            continue
        if s["left"] <= 0:  # full: forget it so a later cancellation alerts again
            if sid in st["notified"]:
                st["notified"].remove(sid)
            continue
        if wanted(s["start"], s["end"]) and sid not in st["notified"]:
            st["notified"].append(sid)
            fresh.append(s)
    if fresh:
        lines = [
            f"{local(s['start']):%a %d %b %H:%M}-{local(s['end']):%H:%M}  "
            f"({s['left']}/{s['max']} courts)"
            for s in fresh
        ]
        notify(
            f"{len(fresh)} badminton slot(s) free at Rivermead",
            "\n".join(lines),
            st["courts"][fresh[0]["fu"]]["url"],
        )
    save_state(st)


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "watch"
    st = load_state()
    if mode == "discover":
        discover(st)
    elif mode == "watch":
        watch_once(st)
    elif mode == "burst":
        end = time.time() + 8 * 60
        while time.time() < end:
            watch_once(st)
            time.sleep(30)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
