#!/usr/bin/env python3
"""Step 2b: ALTER match_shotmap to add 14 missing columns (Kris approved 2026-09-01)."""
import pymysql

def load_env():
    d = {}
    for line in open(".env"):
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            d[k] = v
    return d

def main():
    d = load_env()
    conn = pymysql.connect(
        host=d["MYSQL_HOST"], port=int(d["MYSQL_PORT"]),
        user=d["MYSQL_USER"], password=d["MYSQL_PASSWORD"],
        database=d["MYSQL_DATABASE"], autocommit=False,
    )
    cur = conn.cursor()

    def existing_cols(table):
        cur.execute(f"SHOW COLUMNS FROM {table}")
        return {c[0] for c in cur.fetchall()}

    before = existing_cols("match_shotmap")
    print("BEFORE:", len(before), "cols")

    shotmap_cols = [
        ("player_z", "DECIMAL(6,2)"),
        ("goal_mouth_location", "VARCHAR(30)"),
        ("goal_mouth_x", "DECIMAL(6,2)"),
        ("goal_mouth_y", "DECIMAL(6,2)"),
        ("goal_mouth_z", "DECIMAL(6,2)"),
        ("xgot", "DECIMAL(8,4)"),
        ("block_x", "DECIMAL(6,2)"),
        ("block_y", "DECIMAL(6,2)"),
        ("block_z", "DECIMAL(6,2)"),
        ("goalkeeper_id", "BIGINT"),
        ("goalkeeper_name", "VARCHAR(100)"),
        ("added_time", "INT"),
        ("time_seconds", "INT"),
        ("period_time_seconds", "INT"),
    ]

    added = 0
    skipped = 0
    for col_name, col_type in shotmap_cols:
        if col_name in before:
            skipped += 1
            continue
        if col_name == "player_z":
            sql = f"ALTER TABLE match_shotmap ADD COLUMN player_z DECIMAL(6,2) DEFAULT NULL AFTER player_y"
        else:
            sql = f"ALTER TABLE match_shotmap ADD COLUMN {col_name} {col_type} DEFAULT NULL"
        cur.execute(sql)
        added += 1
        print(f"  added {col_name}")

    conn.commit()
    after = existing_cols("match_shotmap")
    print(f"ADDED={added} SKIPPED={skipped}")
    print("AFTER:", len(after), "cols")
    print("NEW:", sorted(after - before))

    cur.execute("SHOW COLUMNS FROM match_shotmap")
    print("\n=== FINAL match_shotmap column order ===")
    for c in cur.fetchall():
        print(f"  {c[0]:25s} {c[1]}")
    conn.close()

if __name__ == "__main__":
    main()