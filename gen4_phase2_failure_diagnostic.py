#!/usr/bin/env python3
"""Phase 2 failure diagnostic: distinguish timeout vs data-absence.

For each failing comp, sample events and probe the failing endpoints with
HTTP status recording + longer timeout. Goal: determine if failures are
proxy timeouts (fixable) or 404/empty (genuine data absence -> block comp).
"""
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from curl_cffi import requests as cffi_requests

ROOT = Path(__file__).resolve().parent
POOL = []
for l in (ROOT / "data/proxy_pools/good_21.txt").read_text().splitlines():
    if l.strip() and not l.startswith("#"):
        ip, port, u, p = l.strip().split(":", 3)
        POOL.append({"ip": ip, "port": port, "user": u, "pw": p})
i = {"n": 0}

def nxt():
    m = POOL[i["n"] % len(POOL)]
    i["n"] += 1
    return m

def get(path, tries=2, timeout=40):
    for _ in range(tries):
        m = nxt()
        proxy = "http://" + m["user"] + ":" + m["pw"] + "@" + m["ip"] + ":" + m["port"]
        t0 = time.monotonic()
        try:
            r = cffi_requests.get("https://api.sofascore.com/api/v1" + path,
                                  impersonate="chrome124",
                                  proxies={"http": proxy, "https": proxy}, timeout=timeout)
            lat = round((time.monotonic() - t0) * 1000)
            body = r.json() if r.status_code == 200 else None
            # for shotmap body, count shots; for others count top-level keys
            detail = None
            if body is not None:
                if "shotmap" in path:
                    detail = "shots=" + str(len(body.get("shotmap", [])))
                else:
                    detail = "keys=" + str(list(body.keys())[:4])
            return {"http": r.status_code, "lat": lat, "detail": detail, "ip": m["ip"]}
        except Exception as e:
            err = f"{type(e).__name__}: {str(e)[:80]}"
    return {"http": None, "lat": -1, "detail": "EXC " + err, "ip": None}

TARGETS = [
    # (name, ut, season, events to probe)
    ("Emperor's Cup", 323, 96180),
    ("J.League Cup", 101, 97720),
    ("ACL Two", 668, 77009),
    ("Chinese FA Cup", 882, 91152),
    ("K League 1", 410, 88606),
]
ENPS = ["", "/shotmap", "/statistics", "/lineups", "/odds/1/all"]

out = {}
for name, ut, sid in TARGETS:
    print(f"\n=== {name} (ut={ut}, season={sid}) ===", flush=True)
    ev_body = get(f"/unique-tournament/{ut}/season/{sid}/events/last/0")
    events = []
    # we didn't capture body here; do a dedicated body fetch
    m = nxt()
    proxy = "http://" + m["user"] + ":" + m["pw"] + "@" + m["ip"] + ":" + m["port"]
    try:
        r = cffi_requests.get(f"https://api.sofascore.com/api/v1/unique-tournament/{ut}/season/{sid}/events/last/0",
                              impersonate="chrome124", proxies={"http": proxy, "https": proxy}, timeout=40)
        if r.status_code == 200:
            events = [e["id"] for e in r.json().get("events", [])[:3] if isinstance(e, dict)]
    except Exception as e:
        print(f"  enum fail: {e}", flush=True)
    print(f"  sample events: {events}", flush=True)
    comp_res = {}
    for eid in events:
        for tail in ENPS:
            label = tail.lstrip("/") or "event"
            rr = get(f"/event/{eid}{tail}")
            key = label.split("/")[0]
            comp_res.setdefault(key, []).append({"eid": eid, **rr})
            print(f"  {eid} {label:12s} http={rr['http']} lat={rr['lat']}ms {rr['detail'] or ''}", flush=True)
    out[name] = comp_res

Path("/root/.openclaw/workspace/sofascore-backfill/data/phase2_failure_diagnostic.json").write_text(
    json.dumps({"timestamp": datetime.now(timezone.utc).isoformat(), "results": out}, indent=2))
print("\nartifact: data/phase2_failure_diagnostic.json")