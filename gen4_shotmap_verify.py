import pymysql

def load_env():
    d = {}
    for line in open(".env"):
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            d[k] = v
    return d

d = load_env()
pwd = d["MYSQL_PASSWORD"]
conn = pymysql.connect(
    host=d["MYSQL_HOST"], port=int(d["MYSQL_PORT"]),
    user=d["MYSQL_USER"], password=pwd,
    database=d["MYSQL_DATABASE"],
)
cur = conn.cursor()
cur.execute("SELECT COUNT(*), COUNT(DISTINCT match_id), COUNT(DISTINCT player_id) FROM match_shotmap")
r, cnt_mid, cnt_pid = cur.fetchone()
print("total rows:", r, "| distinct match_id:", cnt_mid, "| distinct player_id:", cnt_pid)

cur.execute("SELECT COUNT(*) FROM match_shotmap WHERE player_z IS NOT NULL")
print("player_z populated:", cur.fetchone()[0])
cur.execute("SELECT COUNT(*) FROM match_shotmap WHERE xgot IS NOT NULL")
print("xgot populated:", cur.fetchone()[0])
cur.execute("SELECT COUNT(*) FROM match_shotmap WHERE is_goal=1")
print("goals (is_goal=1):", cur.fetchone()[0])

cur.execute("""
SELECT c.name, COUNT(DISTINCT s.match_id) AS matches, COUNT(*) AS shots
FROM match_shotmap s
JOIN matches m ON m.match_id=s.match_id
JOIN competitions c ON c.competition_id=m.competition_id
GROUP BY c.name ORDER BY matches DESC
""")
print("\n=== per-competition ===")
for row in cur.fetchall():
    print(f"  {row[0]:20s} matches={row[1]:4d} shots={row[2]}")
conn.close()