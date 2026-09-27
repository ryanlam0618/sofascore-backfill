#!/usr/bin/env python3
"""Odds dedup DELETE v4 — task odds-dedup-delete-20260927 (Kris approved (a) 02:15 TG).

v4 fix: v2/v3 TD_SELECT counted MATCHING PAIRS (SUM c(c-1)/2 per group) not
rows-to-delete (SUM(c-1)) — guardrail false-alarm ABORTs (3,259/3,275). Pair-count
reconciliation: groups 2,974+16 c=2 / 5 c=3 / 12 c=5 / 10 c=6 → pairs 3,259 (v2) and
3,275 (v3) EXACT; true rows-to-delete = SUM(c-1) = 3,098. The self-join DELETE deletes
each t1 row exactly once → rowcount = 3,098.
v4: count DISTINCT t1.odds_id; backup SELECT DISTINCT t1.*; DELETE unchanged.

Criteria (GROUND TRUTH, verified 2x via direct GROUP BY):
- Duplicate group: rows identical on all 17 data cols + fetched_at, differ only on odds_id
  (unique row-ID, no PK constraint); D1==D2 → same-fetch-batch true duplicates, zero loss
- Keep lowest odds_id per group, delete the rest
- Movement data (12,221 groups / 95,410 rows): untouched by construction

Guardrails:
- odds_id uniqueness pre-flight
- Backup BEFORE delete
- DELETE rowcount must equal to-delete count exactly, else ROLLBACK
- Movement snapshot counts unchanged after delete
"""
import pymysql, os, json, sys

BACKUP_PATH = "/root/.openclaw/workspace/sofascore-backfill/data/odds_dedup_backup_20260927.sql"
REPORT_PATH = "/root/.openclaw/workspace/sofascore-backfill/data/odds_dedup_20260927_report.json"
EXPECTED_GROUND_TRUTH = 3098   # D1/D2 GROUP BY SUM(c-1), verified 2x
EXPECTED_APPROVED = 3083       # Kris-approved figure (earlier analysis, nv criteria)
TOLERANCE = 0.05

COLS_17 = ["match_id", "market_id", "market_name", "market_group", "market_period",
           "structure_type", "suspended", "choice_name", "initial_fractional_value",
           "fractional_value", "winning", "bookmaker_id", "bookmaker_name", "odds_type",
           "home_odds", "draw_odds", "away_odds"]

cols17_csv = ", ".join(f"`{c}`" for c in COLS_17)
eq_join = " AND ".join([f"t1.`{c}` <=> t2.`{c}`" for c in COLS_17]
                       + ["t1.`fetched_at` <=> t2.`fetched_at`"])
DUP_COND = f"{eq_join} AND t2.`odds_id` < t1.`odds_id`"

TD_COUNT_SQL = f"SELECT COUNT(DISTINCT t1.odds_id) FROM match_odds t1 JOIN match_odds t2 ON {DUP_COND}"
TD_ROWS_SQL = f"SELECT DISTINCT t1.* FROM match_odds t1 JOIN match_odds t2 ON {DUP_COND}"
DELETE_SQL = f"DELETE t1 FROM match_odds t1 JOIN match_odds t2 ON {DUP_COND}"
PER_COMP_SQL = (f"SELECT m.`competition_id`, COUNT(DISTINCT t1.odds_id) FROM match_odds t1 "
                f"JOIN match_odds t2 ON {DUP_COND} "
                f"JOIN matches m ON m.match_id = t1.match_id "
                f"GROUP BY m.`competition_id` ORDER BY 2 DESC")
DUP_COUNT_SQL = f"""
SELECT COUNT(*), COALESCE(SUM(c),0), COALESCE(SUM(c-1),0)
FROM (
  SELECT {cols17_csv}, `fetched_at`, COUNT(*) AS c
  FROM match_odds
  GROUP BY {cols17_csv}, `fetched_at`
  HAVING COUNT(*) > 1
) t
"""
GROUP_SIZE_DIST_SQL = f"""
SELECT c, COUNT(*) FROM (
  SELECT {cols17_csv}, `fetched_at`, COUNT(*) AS c
  FROM match_odds
  GROUP BY {cols17_csv}, `fetched_at`
  HAVING COUNT(*) > 1
) t GROUP BY c ORDER BY c
"""
MOVED_SQL = """
SELECT COUNT(*), COALESCE(SUM(c),0) FROM (
  SELECT `match_id`, `market_id`, `bookmaker_id`, `choice_name`, `odds_type`,
         `market_period`, `structure_type`, COUNT(*) AS c,
         COUNT(DISTINCT CONCAT_WS('|', `initial_fractional_value`, `fractional_value`,
               `home_odds`, `draw_odds`, `away_odds`, `suspended`, `winning`,
               `market_name`, `market_group`)) AS nv
  FROM match_odds GROUP BY `match_id`, `market_id`, `bookmaker_id`, `choice_name`,
       `odds_type`, `market_period`, `structure_type`
  HAVING COUNT(*) > 1
) t WHERE nv > 1
"""

conn = pymysql.connect(
    host=os.environ.get("MYSQL_HOST", "127.0.0.1"),
    port=int(os.environ.get("MYSQL_PORT", "3306")),
    user=os.environ["MYSQL_USER"],
    password=os.environ["MYSQL_PASSWORD"],
    database="appdb",
    charset="utf8mb4",
)
cur = conn.cursor()
report = {"task": "odds-dedup-delete-20260927",
          "approved_by": "Kris 2026-09-27 02:15 TG (option a)",
          "executed_by": "Duncan Main Agent takeover (Forge session killed 02:36 + gateway restart 03:08, dispatches undelivered)",
          "script_version": "v4 (COUNT DISTINCT t1.odds_id; v3 pairs-vs-rows false alarm; v2 CONCAT_WS overcount; v1 fetched_at bug)",
          "criteria": "duplicate group = rows identical on all 17 data cols + fetched_at, differ only on odds_id; keep lowest odds_id, delete rest; movement data untouched"}

# 0. Pre-flight: odds_id uniqueness
cur.execute("SELECT COUNT(*), COUNT(DISTINCT odds_id) FROM match_odds")
n_rows, n_distinct_ids = cur.fetchone()
report["odds_id_unique"] = (n_rows == n_distinct_ids)
print(f"PRE-FLIGHT odds_id: rows={n_rows} distinct={n_distinct_ids} unique={n_rows == n_distinct_ids}")
if n_rows != n_distinct_ids:
    print("ABORT: odds_id NOT unique")
    conn.close()
    sys.exit(1)

# 1. Before counts
cur.execute("SELECT COUNT(*) FROM match_odds")
total_before = cur.fetchone()[0]
cur.execute(DUP_COUNT_SQL)
dup_groups, dup_rows, dup_delete = cur.fetchone()
cur.execute(GROUP_SIZE_DIST_SQL)
size_dist = {int(c): n for c, n in cur.fetchall()}
cur.execute(MOVED_SQL)
mv_groups_b, mv_rows_b = cur.fetchone()
report["before_total"] = total_before
report["dup_groups"] = dup_groups
report["dup_rows_in_groups"] = dup_rows
report["rows_to_delete_expected"] = dup_delete
report["group_size_distribution"] = size_dist
report["moved_groups_before"] = mv_groups_b
report["moved_rows_before"] = mv_rows_b
print(f"BEFORE: total={total_before} dup_groups={dup_groups} dup_rows={dup_rows} to_delete={dup_delete} moved={mv_groups_b}/{mv_rows_b}")
print(f"GROUP_SIZE_DIST: {size_dist}")

# 2. To-delete count (DISTINCT t1 rows — true rows, not pairs)
cur.execute(TD_COUNT_SQL)
td_count = cur.fetchone()[0]
report["to_delete_count"] = td_count
print(f"TO_DELETE (distinct rows): {td_count} (expectation {dup_delete})")
if td_count != dup_delete:
    print(f"ABORT: distinct count {td_count} != GROUP BY expectation {dup_delete}")
    conn.close()
    sys.exit(2)
dev_approved = abs(td_count - EXPECTED_APPROVED) / EXPECTED_APPROVED
report["deviation_vs_approved"] = round(dev_approved, 4)
if dev_approved > TOLERANCE:
    print(f"ABORT: deviation vs approved {dev_approved:.2%} > {TOLERANCE:.0%}")
    conn.close()
    sys.exit(2)
print(f"DEVIATION vs approved 3083: {dev_approved:.2%} ✓ (vs ground truth 3098: exact)")

# 3. Backup BEFORE delete (DISTINCT t1 rows)
cur.execute(TD_ROWS_SQL)
td_rows = cur.fetchall()
columns = [d[0] for d in cur.description]
if len(td_rows) != td_count:
    print(f"ABORT: backup rows {len(td_rows)} != count {td_count}")
    conn.close()
    sys.exit(2)
with open(BACKUP_PATH, "w", encoding="utf-8") as f:
    f.write("-- odds_dedup_backup_20260927.sql\n")
    f.write(f"-- Task odds-dedup-delete-20260927 | Kris approved (a) 02:15 | deleted rows backup ({td_count} rows)\n")
    f.write("-- Rows are true duplicates: identical on all 17 data cols + fetched_at, differ only on odds_id.\n")
    f.write("-- Restore: run these INSERTs to recover deleted rows.\n")
    col_csv = ", ".join(f"`{c}`" for c in columns)
    for r in td_rows:
        vals = []
        for v in r:
            if v is None:
                vals.append("NULL")
            elif isinstance(v, int):
                vals.append(str(v))
            else:
                s = str(v).replace("\\", "\\\\").replace("'", "\\'")
                vals.append(f"'{s}'")
        f.write(f"INSERT INTO match_odds ({col_csv}) VALUES ({', '.join(vals)});\n")
backup_lines = sum(1 for _ in open(BACKUP_PATH, encoding="utf-8"))
report["backup_path"] = BACKUP_PATH
report["backup_rows"] = len(td_rows)
print(f"BACKUP: {BACKUP_PATH} written ({backup_lines} lines incl. header)")

# 4. Per-comp breakdown (pre-delete)
cur.execute(PER_COMP_SQL)
per_comp = {str(k): v for k, v in cur.fetchall()}
report["per_comp_deleted"] = per_comp
print("PER_COMP_DELETED:", per_comp)

# 5. DELETE in single transaction with exact rowcount check
conn.begin()
cur.execute(DELETE_SQL)
deleted = cur.rowcount
print(f"DELETE affected: {deleted}")
if deleted != td_count:
    conn.rollback()
    report["status"] = "aborted_rollback"
    report["deleted"] = deleted
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"ABORT+ROLLBACK: deleted {deleted} != to_delete {td_count}")
    conn.close()
    sys.exit(3)
conn.commit()
report["deleted"] = deleted
report["status"] = "deleted_committed"

# 6. Post-commit verification
cur.execute("SELECT COUNT(*) FROM match_odds")
total_after = cur.fetchone()[0]
cur.execute(DUP_COUNT_SQL)
dup_groups_a, _, _ = cur.fetchone()
cur.execute(MOVED_SQL)
mv_groups_a, mv_rows_a = cur.fetchone()
report["after_total"] = total_after
report["dup_groups_after"] = dup_groups_a
report["moved_groups_after"] = mv_groups_a
report["moved_rows_after"] = mv_rows_a
print(f"AFTER: total={total_after} (delta={total_before - total_after}) dup_groups={dup_groups_a} moved={mv_groups_a}/{mv_rows_a}")

ok = (total_after == total_before - deleted
      and dup_groups_a == 0
      and mv_groups_a == mv_groups_b and mv_rows_a == mv_rows_b)
report["verify_passed"] = ok
print("VERIFY:", "PASSED" if ok else "FAILED")

with open(REPORT_PATH, "w", encoding="utf-8") as f:
    json.dump(report, f, ensure_ascii=False, indent=2)
print(f"REPORT: {REPORT_PATH}")
conn.close()
print("DEDUP_DONE" if ok else "DEDUP_VERIFY_FAILED")
