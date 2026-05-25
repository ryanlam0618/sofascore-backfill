#!/usr/bin/env python3
"""
Referee backfill: extract referee data from /api/v1/event/{event_id} API.
Reads event_ids from sofascore_incident_events, calls event API, stores to sofascore_referees.
Uses rotating proxy pool with retry.
"""
from __future__ import annotations
import argparse, json, os, random, sys, time, urllib.request, urllib.error
from datetime import datetime, timezone

WORKDIR = "/root/.openclaw/workspace/sofascore_backfill"
PROXY_FILE = f"{WORKDIR}/data/proxy_list.txt"
API_BASE = "https://www.sofascore.com/api/v1"

sys.path.insert(0, WORKDIR)
from mysql_helpers import ensure_mysql_tables, mysql_connect

def load_proxies():
    with open(PROXY_FILE) as f:
        lines = [l.strip() for l in f if l.strip()]
    proxies = []
    for line in lines:
        parts = line.rsplit(":", 2)
        proxies.append((parts[0], f"{parts[1]}:{parts[2]}"))
    random.shuffle(proxies)
    return proxies

PROXIES = load_proxies()
random.shuffle(PROXIES)
proxy_iter = iter(PROXIES)

def get_proxy():
    global proxy_iter
    try:
        return next(proxy_iter)
    except StopIteration:
        random.shuffle(PROXIES)
        proxy_iter = iter(PROXIES)
        return next(proxy_iter)

def now_mysql():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

def random_ua():
    return random.choice([
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/123.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    ])

def to_int(v, default=None):
    try: return int(v)
    except: return default

def fetch_event(event_id):
    url = f"{API_BASE}/event/{event_id}"
    headers = {
        "User-Agent": random_ua(),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6,zh-HK;q=0.5",
        "Connection": "close",
    }
    req = urllib.request.Request(url, headers=headers)
    for attempt in range(3):
        ip_port, auth = get_proxy()
        proxy_url = f"http://{auth}@{ip_port}"
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}))
            with opener.open(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 402 and attempt < 2:
                time.sleep(0.3)
                continue
            return e.code, {}
        except Exception:
            if attempt < 2:
                time.sleep(0.3)
                continue
            return 500, {}
    return 500, {}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--retry-failed", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--sleep-min", type=float, default=0.6)
    ap.add_argument("--sleep-max", type=float, default=1.2)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--checkpoint-every", type=int, default=200)
    args = ap.parse_args()

    conn = mysql_connect()
    ensure_mysql_tables(conn)
    print(f"[INFO] Referee backfill, proxies={len(PROXIES)}")

    # Use autocommit to avoid lock waits from long-held transaction
    conn.autocommit = True

    cur = conn.cursor(dictionary=True)

    if args.retry_failed:
        cur.execute("""
            SELECT DISTINCT e.event_id
            FROM sofascore_incident_events e
            INNER JOIN sofascore_referees_fetch_log r ON e.event_id = r.event_id
            WHERE r.status_code != 200 OR r.referee_id IS NULL
            ORDER BY e.event_id
        """)
    elif args.all:
        cur.execute("SELECT DISTINCT event_id FROM sofascore_incident_events ORDER BY event_id")
    else:
        cur.execute("""
            SELECT DISTINCT e.event_id
            FROM sofascore_incident_events e
            WHERE NOT EXISTS (
                SELECT 1 FROM sofascore_referees_fetch_log r
                WHERE r.event_id = e.event_id AND r.status_code = 200 AND r.referee_id IS NOT NULL
            )
            ORDER BY e.event_id
        """)
    targets = [r["event_id"] for r in cur.fetchall()]
    cur.close()

    total = len(targets)
    if args.limit and args.limit > 0:
        targets = targets[:args.limit]
        total = len(targets)

    print(f"[INFO] targets={total}")
    if total == 0:
        conn.close()
        return

    ok = errors = not_found = 0
    fetched_at = now_mysql()

    for i, event_id in enumerate(targets, 1):
        status_code, data = fetch_event(event_id)
        err = ""
        ref_id = None

        if status_code == 200:
            ev = data.get("event", {})
            referee = ev.get("referee", {}) or {}
            ref_id = to_int(referee.get("id"))
            if ref_id:
                cur2 = conn.cursor()
                country = referee.get("country") or {}
                cur2.execute("""
                    INSERT INTO sofascore_referees
                    (referee_id, name, short_name, slug, country_alpha2, country_name,
                     matches_total, yellow_cards_total, red_cards_total, yellow_red_cards_total, fetched_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON DUPLICATE KEY UPDATE
                      name=VALUES(name), short_name=VALUES(short_name), slug=VALUES(slug),
                      country_alpha2=VALUES(country_alpha2), country_name=VALUES(country_name),
                      matches_total=VALUES(matches_total), yellow_cards_total=VALUES(yellow_cards_total),
                      red_cards_total=VALUES(red_cards_total), yellow_red_cards_total=VALUES(yellow_red_cards_total),
                      fetched_at=VALUES(fetched_at)
                """, (
                    ref_id,
                    referee.get("name", ""),
                    referee.get("shortName", "") if isinstance(referee, dict) else "",
                    referee.get("slug", ""),
                    country.get("alpha2", "") if isinstance(country, dict) else "",
                    country.get("name", "") if isinstance(country, dict) else "",
                    to_int(referee.get("games")),
                    to_int(referee.get("yellowCards")),
                    to_int(referee.get("redCards")),
                    to_int(referee.get("yellowRedCards")),
                    fetched_at,
                ))
                cur2.close()
                ok += 1
            else:
                not_found += 1

            cur2 = conn.cursor()
            cur2.execute("""
                INSERT INTO sofascore_referees_fetch_log (event_id, referee_id, status_code, fetched_at, error)
                VALUES (%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE status_code=VALUES(status_code), referee_id=VALUES(referee_id), fetched_at=VALUES(fetched_at)
            """, (event_id, ref_id, status_code, fetched_at, err))
            cur2.close()
        else:
            err = f"HTTP {status_code}"
            errors += 1
            cur2 = conn.cursor()
            cur2.execute("""
                INSERT INTO sofascore_referees_fetch_log (event_id, referee_id, status_code, fetched_at, error)
                VALUES (%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE status_code=VALUES(status_code), fetched_at=VALUES(fetched_at), error=VALUES(error)
            """, (event_id, None, status_code, fetched_at, err))
            cur2.close()

        if (i % args.checkpoint_every == 0) or (i == total):
            conn.commit()
            pct = (i / total * 100) if total else 100
            print(f"[PROGRESS] {i}/{total} ({pct:.1f}%) ok={ok} err={errors} no_ref={not_found}", flush=True)

        time.sleep(random.uniform(args.sleep_min, args.sleep_max))

    conn.commit()
    conn.close()
    print(f"[DONE] ok={ok} err={errors} no_ref={not_found}")

if __name__ == "__main__":
    main()