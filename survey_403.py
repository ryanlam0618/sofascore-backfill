#!/usr/bin/env python3
import sqlite3, urllib.request, urllib.error, json, time

db = sqlite3.connect("data/backfill_sofascore_10y/lineups_PL.sqlite")
error_ids = [r[0] for r in db.execute("SELECT event_id FROM lineup_events WHERE status_code = 403").fetchall()]

headers = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Encoding": "identity",
    "Referer": "https://www.sofascore.com/",
}

retriable = []
not_found = []
blocked = []

total = len(error_ids)
for i, eid in enumerate(error_ids):
    url = f"https://widgets.sofascore.com/embed/lineups?id={eid}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status == 200:
                retriable.append(eid)
                print(f"[{i+1}/{total}] {eid}: 200 ✅")
            else:
                blocked.append((eid, resp.status))
                print(f"[{i+1}/{total}] {eid}: HTTP {resp.status}")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            not_found.append(eid)
            print(f"[{i+1}/{total}] {eid}: 404 (archived)")
        else:
            blocked.append((eid, e.code))
            print(f"[{i+1}/{total}] {eid}: HTTP {e.code} (blocked)")
    except Exception as ex:
        blocked.append((eid, str(ex)))
        print(f"[{i+1}/{total}] {eid}: ERROR {ex}")
    time.sleep(0.3)

result = {
    "total": total,
    "retriable": retriable,
    "not_found": not_found,
    "blocked": blocked,
}

with open("data/backfill_sofascore_10y/lineups_403_survey.json", "w") as f:
    json.dump(result, f, indent=2)

print(f"\n=== SUMMARY ===")
print(f"Total 403 events: {total}")
print(f"✅ Retriable (200 + lineups): {len(retriable)}")
print(f"❌ Not Found (404): {len(not_found)}")
print(f"⚠️  Blocked (403/etc): {len(blocked)}")
print(f"Saved to data/backfill_sofascore_10y/lineups_403_survey.json")
