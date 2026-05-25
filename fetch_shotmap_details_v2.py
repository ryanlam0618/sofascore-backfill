#!/usr/bin/env python3
"""
Shotmap details backfill with proxy pool rotation.
- Reads failed events from sofascore_shotmap_detail_events (HTTP 500/403)
- Fetches /api/v1/event/{event_id}/shotmap with rotating proxies
- Stores per-shot rows in sofascore_shotmap_details

Retry mode: re-fetches events that previously failed (status_code != 200)
"""
from __future__ import annotations
import argparse, json, os, random, sys, time, urllib.request, urllib.error
from datetime import datetime, timezone
from pathlib import Path

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
        ip_port = parts[0]
        auth = f"{parts[1]}:{parts[2]}"
        proxies.append((ip_port, auth))
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
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:125.0) Gecko/20100101 Firefox/125.0",
    ])

def fetch_shotmap(event_id):
    url = f"{API_BASE}/event/{event_id}/shotmap"
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

def to_int(v, default=None):
    try: return int(v)
    except: return default

def to_float(v, default=None):
    try: return float(v)
    except: return default

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--retry-failed", action="store_true", help="Retry events with status_code != 200")
    ap.add_argument("--all", action="store_true", help="Process all has_shotmap=1 events")
    ap.add_argument("--sleep-min", type=float, default=0.8)
    ap.add_argument("--sleep-max", type=float, default=1.5)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--checkpoint-every", type=int, default=100)
    args = ap.parse_args()

    conn = mysql_connect()
    ensure_mysql_tables(conn)
    print(f"[INFO] Using MySQL, proxies={len(PROXIES)}")

    cur = conn.cursor(dictionary=True)

    # Determine targets
    if args.retry_failed:
        cur.execute("""
            SELECT DISTINCT xg.event_id
            FROM sofascore_shotmap_xg_backfill xg
            INNER JOIN sofascore_shotmap_detail_events de ON xg.event_id = de.event_id
            WHERE xg.has_shotmap = 1 AND de.status_code != 200
            ORDER BY xg.event_id
        """)
    elif args.all:
        cur.execute("""
            SELECT DISTINCT xg.event_id
            FROM sofascore_shotmap_xg_backfill xg
            WHERE xg.has_shotmap = 1
            ORDER BY xg.event_id
        """)
    else:
        cur.execute("""
            SELECT DISTINCT xg.event_id
            FROM sofascore_shotmap_xg_backfill xg
            INNER JOIN sofascore_shotmap_detail_events de ON xg.event_id = de.event_id
            WHERE xg.has_shotmap = 1 AND de.status_code = 200
              AND NOT EXISTS (
                  SELECT 1 FROM sofascore_shotmap_details sd
                  WHERE sd.event_id = xg.event_id LIMIT 1
              )
            ORDER BY xg.event_id
        """)
    targets = [r["event_id"] for r in cur.fetchall()]
    cur.close()

    total = len(targets)
    if args.limit and args.limit > 0:
        targets = targets[:args.limit]
        total = len(targets)

    print(f"[INFO] targets={total} (retry_failed={args.retry_failed}, all={args.all})")
    if total == 0:
        print("[INFO] Nothing to do")
        conn.close()
        return

    ok = errors = not_found = 0
    rows_upserted = 0
    fetched_at = now_mysql()

    for i, event_id in enumerate(targets, 1):
        status_code, payload = fetch_shotmap(event_id)
        shots = []
        err = ""

        if status_code == 200:
            shots = (payload or {}).get("shotmap", [])
            cur2 = conn.cursor()
            for sh in shots:
                player_info = sh.get("player", {}) or {}
                team_info = sh.get("team", {}) or {}
                coords = sh.get("playerCoordinates") or sh.get("coordinates") or {}
                cur2.execute("""
                    INSERT INTO sofascore_shotmap_details
                    (event_id, shot_id, is_home_shot, team_id, team_name,
                     player_id, player_name, player_position,
                     minute, added_time, time_seconds,
                     incident_type, shot_type, situation, body_part, goal_mouth_location,
                     player_x, player_y, xg,
                     home_team_goal_prob, away_team_goal_prob, fetched_at, error)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON DUPLICATE KEY UPDATE
                      is_home_shot=VALUES(is_home_shot), team_id=VALUES(team_id), team_name=VALUES(team_name),
                      player_id=VALUES(player_id), player_name=VALUES(player_name), player_position=VALUES(player_position),
                      minute=VALUES(minute), added_time=VALUES(added_time), time_seconds=VALUES(time_seconds),
                      incident_type=VALUES(incident_type), shot_type=VALUES(shot_type), situation=VALUES(situation),
                      body_part=VALUES(body_part), goal_mouth_location=VALUES(goal_mouth_location),
                      player_x=VALUES(player_x), player_y=VALUES(player_y), xg=VALUES(xg),
                      home_team_goal_prob=VALUES(home_team_goal_prob), away_team_goal_prob=VALUES(away_team_goal_prob),
                      fetched_at=VALUES(fetched_at), error=VALUES(error)
                """, (
                    event_id, to_int(sh.get("id")),
                    1 if sh.get("isHome") is True else 0 if sh.get("isHome") is False else None,
                    to_int(team_info.get("id") if isinstance(team_info, dict) else None),
                    team_info.get("name") if isinstance(team_info, dict) else None,
                    to_int(player_info.get("id") if isinstance(player_info, dict) else None),
                    player_info.get("name") if isinstance(player_info, dict) else None,
                    player_info.get("position") if isinstance(player_info, dict) else None,
                    to_int(sh.get("time")), to_int(sh.get("addedTime")), to_int(sh.get("timeSeconds")),
                    sh.get("incidentType"), sh.get("shotType"), sh.get("situation"),
                    sh.get("bodyPart"), sh.get("goalMouthLocation"),
                    to_float(coords.get("x") if isinstance(coords, dict) else None),
                    to_float(coords.get("y") if isinstance(coords, dict) else None),
                    to_float(sh.get("xg")),
                    to_float(sh.get("homeTeamGoalProbability")),
                    to_float(sh.get("awayTeamGoalProbability")),
                    fetched_at, "",
                ))
                rows_upserted += 1
            cur2.execute("""
                INSERT INTO sofascore_shotmap_detail_events
                (event_id, status_code, shot_count, fetched_at, error)
                VALUES (%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE status_code=VALUES(status_code),
                  shot_count=VALUES(shot_count), fetched_at=VALUES(fetched_at), error=VALUES(error)
            """, (event_id, status_code, len(shots), fetched_at, ""))
            cur2.close()
            ok += 1
        elif status_code == 404:
            cur2 = conn.cursor()
            cur2.execute("""
                INSERT INTO sofascore_shotmap_detail_events (event_id, status_code, shot_count, fetched_at, error)
                VALUES (%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE status_code=VALUES(status_code), fetched_at=VALUES(fetched_at)
            """, (event_id, status_code, 0, fetched_at, "no_shotmap_data"))
            cur2.close()
            not_found += 1
        else:
            err = f"HTTP {status_code}"
            cur2 = conn.cursor()
            cur2.execute("""
                INSERT INTO sofascore_shotmap_detail_events (event_id, status_code, shot_count, fetched_at, error)
                VALUES (%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE status_code=VALUES(status_code), fetched_at=VALUES(fetched_at), error=VALUES(error)
            """, (event_id, status_code, 0, fetched_at, err))
            cur2.close()
            errors += 1

        if (i % args.checkpoint_every == 0) or (i == total):
            conn.commit()
            pct = (i / total * 100) if total else 100
            print(f"[PROGRESS] {i}/{total} ({pct:.1f}%) ok={ok} err={errors} 404={not_found} shots={rows_upserted}", flush=True)

        time.sleep(random.uniform(args.sleep_min, args.sleep_max))

    conn.commit()
    conn.close()
    print(f"[DONE] ok={ok} err={errors} 404={not_found} shots_upserted={rows_upserted}")

if __name__ == "__main__":
    main()