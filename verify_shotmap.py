import pymysql
d = {}
for line in open(".env"):
    line = line.strip()
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1)
        d[k] = v
pw = d["MYSQL_PASSWORD"]
conn = pymysql.connect(
    host=d["MYSQL_HOST"], port=int(d["MYSQL_PORT"]),
    user=d["MYSQL_USER"], password=pw,
    database=d["MYSQL_DATABASE"],
)
cur = conn.cursor()
cur.execute("SELECT COUNT(*), COUNT(DISTINCT match_id) FROM match_shotmap")
r, c = cur.fetchone()
print(f"match_shotmap final: rows={r}, distinct_match_id={c}")
cur.execute("SELECT COUNT(*) FROM match_shotmap WHERE player_z IS NOT NULL")
print("player_z populated:", cur.fetchone()[0])
cur.execute("SELECT COUNT(*) FROM match_shotmap WHERE xgot IS NOT NULL")
print("xgot populated:", cur.fetchone()[0])
conn.close()