#!/usr/bin/env python3
"""
Attendance scrape with rotating proxy pool (100 proxies).
Auto-failover on 402/timeout, no manual intervention needed.
"""
from __future__ import annotations
import json, random, sys, time, urllib.request, urllib.error
from datetime import datetime, timezone

WORKDIR = "/root/.openclaw/workspace/sofascore_backfill"
AUTH = "aeptenjc:dztr57tcycoz"
PROXY_FILE = f"{WORKDIR}/data/proxy_list.txt"

def load_proxies():
    with open(PROXY_FILE) as f:
        # Format: IP:PORT:USERNAME:PASSWORD → extract IP:PORT
        return [line.strip().rsplit(':', 2)[0] for line in f if line.strip()]

PROXIES_POOL = load_proxies()
random.shuffle(PROXIES_POOL)
PROXY_ITER = iter(PROXIES_POOL)

def get_next_proxy():
    """Round-robin through proxy pool, shuffle when exhausted."""
    global PROXY_ITER
    try:
        p = next(PROXY_ITER)
        return p  # already stripped to IP:PORT
    except StopIteration:
        random.shuffle(PROXIES_POOL)
        PROXY_ITER = iter(PROXIES_POOL)
        return next(PROXY_ITER)

def now_mysql():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

def parse_attendance(raw) -> int | None:
    if raw is None or raw == "" or raw == "–":
        return None
    if isinstance(raw, int):
        return raw if raw > 0 else None
    s = str(raw).replace(",", "").strip()
    try:
        v = int(s)
        return v if v > 0 else None
    except:
        return None

def fetch_html(event_id: int) -> dict:
    """Fetch event HTML with rotating proxy. 4s timeout."""
    url = f"https://www.sofascore.com/event/{event_id}"
    headers = {
        "User-Agent": random.choice([
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/123.0.0.0 Safari/537.36",
        ]),
        "Accept": "text/html",
        "Connection": "close",
    }
    req = urllib.request.Request(url, headers=headers)

    # Try up to 3 proxies
    for attempt in range(3):
        proxy = get_next_proxy()
        proxy_url = f"http://{AUTH}@{proxy}"
        proxies_d = {"http": proxy_url, "https": proxy_url}

        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler(proxies_d))
            resp = opener.open(req, timeout=4)
            html = resp.read().decode("utf-8", errors="ignore")

            nd_idx = html.find("__NEXT_DATA__")
            if nd_idx < 0:
                return {"status": "no_data", "att": None}

            jstart = html.find("{", nd_idx)
            bc = 0
            for i, c in enumerate(html[jstart:]):
                if c == "{": bc += 1
                elif c == "}": bc -= 1
                if bc == 0:
                    data = json.loads(html[jstart:jstart+i+1])
                    break
            else:
                return {"status": "json_err", "att": None}

            ev = data.get("props", {}).get("pageProps", {})
            ip = ev.get("initialProps", ev) or {}
            ev2 = ip.get("event", {})

            if not ev2:
                return {"status": "no_event", "att": None}

            raw = ev2.get("attendance")
            att = parse_attendance(raw)
            return {"status": 200, "att": att, "raw": str(raw) if raw else ""}

        except urllib.error.HTTPError as e:
            if e.code == 402:
                # Proxy quota exhausted — try next proxy immediately
                if attempt < 2:
                    time.sleep(0.5)
                    continue
            return {"status": f"http_{e.code}", "att": None}

        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < 2:
                time.sleep(0.5)
                continue
            return {"status": "timeout", "att": None}

        except Exception as e:
            if attempt < 2:
                time.sleep(0.5)
                continue
            return {"status": f"err_{type(e).__name__}", "att": None}

    return {"status": "exhausted_3", "att": None}


def main():
    sys.path.insert(0, WORKDIR)
    from fetch_incidents import mysql_connect

    conn = mysql_connect()
    cur = conn.cursor()

    cur.execute("""
        SELECT DISTINCT e.event_id
        FROM sofascore_incident_events e
        LEFT JOIN sofascore_attendance_fetch_log a ON a.event_id = e.event_id
        WHERE e.match_date IS NOT NULL AND e.match_date != ""
        AND (a.event_id IS NULL OR a.status_code != 200 OR a.attendance_int IS NULL)
        ORDER BY e.event_id
    """)
    targets = [r[0] for r in cur.fetchall()]
    total = len(targets)

    if total == 0:
        print("[INFO] All done!")
        cur.close()
        conn.close()
        return

    print(f"[INFO] {total} events to scrape using {len(PROXIES_POOL)} proxies", flush=True)

    ok = errors = no_att = 0
    fetched_at = now_mysql()
    status_counts = {}

    for i, event_id in enumerate(targets, 1):
        result = fetch_html(event_id)
        status = str(result["status"])
        status_counts[status] = status_counts.get(status, 0) + 1

        att = result.get("att")
        raw = result.get("raw", "")

        if result["status"] == 200:
            cur2 = conn.cursor()
            cur2.execute("""
                INSERT INTO sofascore_attendance_fetch_log
                (event_id, fetched_at, status_code, attendance_raw, attendance_int, error)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    fetched_at=VALUES(fetched_at),
                    status_code=VALUES(status_code),
                    attendance_raw=VALUES(attendance_raw),
                    attendance_int=VALUES(attendance_int),
                    error=VALUES(error)
            """, (event_id, fetched_at, 200, raw, att, "no_att" if att is None else ""))
            conn.commit()
            cur2.close()
            if att is not None:
                ok += 1
            else:
                no_att += 1
        else:
            errors += 1

        time.sleep(random.uniform(0.4, 0.8))

        if i % 100 == 0 or i == total:
            print(f"[PROGRESS] {i}/{total} ok={ok} err={errors} no_att={no_att}", flush=True)

    print(f"[DONE] ok={ok}, err={errors}, no_att={no_att}", flush=True)
    print(f"Status: {dict(sorted(status_counts.items(), key=lambda x: str(x[0])))}", flush=True)

    cur.close()
    conn.close()

if __name__ == "__main__":
    main()