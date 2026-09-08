#!/usr/bin/env python3
"""Post-run recovery row-count diff."""
import mysql.connector
from dotenv import load_dotenv
from pathlib import Path
import os

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
pwd = os.environ.get("MYSQL_PASSWORD", "").strip()
conn = mysql.connector.connect(
    host="127.0.0.1", port=3306, user="appdb_rw",
    password=pwd, database="appdb", autocommit=True,
)
cur = conn.cursor()

pre = {"match_lineups": 158288, "match_odds": 128689, "match_player_stats": 13379}

print("=== FULL TABLE POST-RUN ===")
for t in ("match_lineups", "match_odds", "match_player_stats", "match_momentum", "match_average_positions"):
    cur.execute(f"SELECT COUNT(*) FROM {t}")
    post = cur.fetchone()[0]
    delta = post - pre[t] if t in pre else "n/a"
    print(f"{t}: post={post} delta_vs_pre={delta}")

# Per-target table verification for the 3 deadlock endpoint tables
print("\n=== DEADLOCK TARGET VERIFICATION (should all be >0 now) ===")
odds = [13274714,13379058,13482080,13980083,13980084,13981633,14021230,14021238,14024005,14028329,14054071,14064409,14065218,14065228,14065739,14081813,14083191,14083422,14250633,14250660,14461753,14566737,14566951,14572782,14573075,14641638,14888320,15171821,15372955,15453077,15496189,15551888,15632631]
avgp=[14566928,14572753,14572781]; mom=[14083687]
zeroods=[m for m in odds if cur.execute("SELECT COUNT(*) FROM match_odds WHERE match_id=%s",(m,)) or cur.fetchone()[0]==0]
print("odds targets with 0 rows:", len(zeroods), zeroods)
z2=[(m,cur.execute("SELECT COUNT(*) FROM match_average_positions WHERE match_id=%s",(m,)) or cur.fetchone()[0]) for m in avgp]
print("avg_pos target counts:", z2)
cur.execute("SELECT COUNT(*) FROM match_momentum WHERE match_id=%s",(mom[0],)); print("momentum target count:", cur.fetchone()[0])

# lineups: verify all 169 targeted now have match_player_stats rows
line_ids="14025781,14028287,14054071,14054072,14054074,14054075,14054076,14054083,14054087,14073447,14073452,14073454,14073462,14073463,14073466,14073468,14073470,14073474,14149963,14250660,14250673,14270430,14353429,14353437,14377055,14396153,14411087,14424510,14425847,14425848,14425853,14426851,14426926,14493422,14493424,14572820,14573080,14585923,14585925,14585926,14586193,14598940,14598953,14763685,14787850,14843662,14843682,14843688,14843689,14843690,14843691,14843696,14843697,14843698,14843699,14843700,14843703,14843704,14843705,14843706,14843707,14843708,14843709,14843711,14843713,14843714,14843715,14843717,14843718,14843723,14843724,14843725,14843727,14843728,14843729,14888310,14888330,14970878,14994243,15002171,15002174,15037707,15037708,15037713,15037714,15037715,15037716,15037717,15037720,15037722,15037723,15037724,15037725,15037727,15037728,15037729,15037731,15037732,15037734,15037735,15171799,15171801,15171825,15171827,15193715,15197268,15197272,15197276,15197280,15197284,15197286,15197287,15200514,15200526,15200527,15262365,15262370,15262376,15296007,15298464,15327740,15327741,15359733,15359740,15359743,15372917,15380216,15426968,15426971,15426974,15440056,15453077,15453088,15453099,15479165,15492898,15492899,15492900,15532694,15538902,15552624,15556601,15579437,15600442,15631837,15631842,15632053,15664537,15697561,15755701,15755725,15884736,15884737,15884781,16040850,16040862,16074214,16114123,16173051,16222684,16222685,16222690,16222693,16222694,16222696,16411486,16411487,16411489,16411490"
ids=[int(x) for x in line_ids.split(",")]
cur.execute("SELECT match_id, COUNT(*) FROM match_player_stats WHERE match_id IN (%s) GROUP BY match_id" % ",".join(["%s"]*len(ids)), ids)
got=dict(cur.fetchall())
missing=[m for m in ids if got.get(m,0)==0]
print("\nlineups targets w/ 0 match_player_stats rows:", len(missing), missing[:20])

# the pass2 10 events all recovered
print("\n=== PASS2 (10) targeted recovery rows ===")
pass2=[14073447,14377055,15037713,15037734,14073454,14585926,14843699,14843723,15440056,15632053]
cur.execute("SELECT match_id, COUNT(*) FROM match_player_stats WHERE match_id IN (%s) GROUP BY match_id" % ",".join(["%s"]*len(pass2)), pass2)
print(dict(cur.fetchall()))
conn.close()