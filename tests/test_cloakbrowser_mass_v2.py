#!/usr/bin/env python3
"""
Mass CloakBrowser Test V2 — Network Capture Method
====================================================
24 competitions × 10-11 seasons × 1 event per season.
Event ID discovery: direct (no proxy).
Data fetch: CloakBrowser + proxy, network capture (visit page, intercept API).
Retry: auto-relaunch on bad IP (~40% of WebShare IPs flagged by SofaScore).
"""

import json, time, sys, os, subprocess, tempfile, logging
from pathlib import Path
from collections import defaultdict

env_path = Path(__file__).resolve().parent.parent / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

SEASON_IDS_FILE = str(Path(__file__).resolve().parent.parent / "data" / "cloak_test_season_ids.json")
RESULTS_FILE = str(Path(__file__).resolve().parent.parent / "data" / "cloak_mass_v2_results.json")
PYTHON = "/root/.openclaw/workspace/sofascore-backfill/.runner-venv/bin/python3"

MAX_IP_RETRIES = 6
COOLDOWN = 3
DELAY_BETWEEN_EVENTS = 1

# --- Workers (standalone scripts, load .env themselves) ---

FETCH_WORKER = r'''#!/usr/bin/env python3
import json, time, sys, os
from pathlib import Path
env = Path('/root/.openclaw/workspace/sofascore-backfill/.env')
if env.exists():
    for ln in env.read_text().splitlines():
        if '=' in ln and not ln.startswith('#'):
            k,v = ln.split('=',1); os.environ.setdefault(k.strip(), v.strip())
sys.path.insert(0, '/root/.openclaw/workspace/sofascore-backfill/.runner-venv/lib/python3.11/site-packages')
from cloakbrowser import launch
PROXY={"server":f"http://{os.getenv('SOFA_PROXY_HOST','p.webshare.io')}:{os.getenv('SOFA_PROXY_PORT','80')}","username":os.getenv('SOFA_PROXY_USER',''),"password":os.getenv('SOFA_PROXY_PASS','')}
EID=int(sys.argv[1]); OUT=sys.argv[2]; EID_S=str(EID)
browser=launch(headless=True,humanize=True,proxy=PROXY)
ctx=browser.new_context(); page=ctx.new_page()
api_data={}; api_st={}
def cap(r):
    u=r.url
    if '/api/v1/' in u and 'sofascore.com' in u:
        try:
            s=r.status; p=u.rstrip('/').split('/api/v1/'); ep=p[1] if len(p)>1 else '?'
            if s==200:
                try: api_data[ep]=r.json()
                except: pass
            api_st[ep]=s
        except: pass
page.on('response',cap)
try:
    resp=page.goto(f'https://www.sofascore.com/event/{EID}',timeout=45000); ps=resp.status if resp else 0
except: ps=0
time.sleep(8)
ok=sum(1 for s in api_st.values() if s==200); f403=sum(1 for s in api_st.values() if s==403)
cov=-99; home='?'; away='?'
for ep,d in api_data.items():
    if EID_S in ep and isinstance(d,dict):
        ev=d.get('event',d); cov=ev.get('coverage',-99)
        ht=ev.get('homeTeam',{}); at=ev.get('awayTeam',{})
        home=ht.get('name','?') if isinstance(ht,dict) else '?'
        away=at.get('name','?') if isinstance(at,dict) else '?'
        break
lu=api_st.get(f'event/{EID_S}/lineups',0)
sm=api_st.get(f'event/{EID_S}/shotmap',0)
inc=api_st.get(f'event/{EID_S}/incidents',0)
graph=api_st.get(f'event/{EID_S}/graph',0)
lu_p=0
ld=api_data.get(f'event/{EID_S}/lineups')
if ld and isinstance(ld,dict):
    for t in ld.get('lineup',[]): lu_p+=len(t.get('players',[]))
sm_s=0
sd=api_data.get(f'event/{EID_S}/shotmap')
if sd and isinstance(sd,dict): sm_s=len(sd.get('shotmap',[]))
r={"event_id":EID,"page_status":ps,"api_ok":ok,"api_403":f403,"coverage":cov,
   "home":home,"away":away,"lineups":lu,"shotmap":sm,"incidents":inc,"graph":graph,
   "lineups_players":lu_p,"shotmap_shots":sm_s,"endpoints":list(api_data.keys())}
with open(OUT,'w') as f: json.dump(r,f)
try: page.close(); ctx.close(); browser.close()
except: pass
'''

FIND_WORKER = r'''#!/usr/bin/env python3
import json, sys
sys.path.insert(0, '/root/.openclaw/workspace/sofascore-backfill/.runner-venv/lib/python3.11/site-packages')
from cloakbrowser import launch
UT=int(sys.argv[1]); SID=int(sys.argv[2]); OUT=sys.argv[3]
browser=launch(headless=True,humanize=True)
page=browser.new_page()
eid=None; home='?'; away='?'
for r in [1,2,3]:
    u=f'https://www.sofascore.com/api/v1/unique-tournament/{UT}/season/{SID}/events/round/{r}'
    try:
        resp=page.goto(u,timeout=20000)
        if resp and resp.status==200:
            b=page.evaluate("() => document.body.innerText")
            d=json.loads(b); evs=d.get('events',[])
            if evs:
                ev=evs[0]; eid=ev.get('id')
                ht=ev.get('homeTeam',{}); at=ev.get('awayTeam',{})
                home=ht.get('name','?') if isinstance(ht,dict) else '?'
                away=at.get('name','?') if isinstance(at,dict) else '?'
                break
    except: pass
if eid is None:
    u=f'https://www.sofascore.com/api/v1/unique-tournament/{UT}/season/{SID}/cuptrees'
    try:
        resp=page.goto(u,timeout=20000)
        if resp and resp.status==200:
            b=page.evaluate("() => document.body.innerText")
            d=json.loads(b)
            for t in d.get('cupTrees',[]):
                evs=t.get('events',[])
                if evs:
                    ev=evs[0]; eid=ev.get('id')
                    ht=ev.get('homeTeam',{}); at=ev.get('awayTeam',{})
                    home=ht.get('name','?') if isinstance(ht,dict) else '?'
                    away=at.get('name','?') if isinstance(at,dict) else '?'
                    break
    except: pass
with open(OUT,'w') as f: json.dump({"event_id":eid,"home":home,"away":away},f)
try: page.close(); browser.close()
except: pass
'''

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("v2")

def run_worker(script, args, timeout=120):
    fd, sp = tempfile.mkstemp(suffix='.py')
    with os.fdopen(fd,'w') as f: f.write(script)
    out = tempfile.mktemp(suffix='.json')
    try:
        subprocess.run([PYTHON, sp]+[str(a) for a in args]+[out], capture_output=True, text=True, timeout=timeout)
        if os.path.exists(out):
            with open(out) as f: return json.load(f)
    except: pass
    return None

def fetch_event(eid):
    for attempt in range(1, MAX_IP_RETRIES+1):
        r = run_worker(FETCH_WORKER, [eid])
        if r and r.get("api_ok",0) >= 10:
            r["retries"] = attempt-1
            return r
        elif r:
            log.warning(f"    try {attempt}: bad IP (ok={r.get('api_ok',0)}, 403={r.get('api_403',0)})")
        else:
            log.warning(f"    try {attempt}: crashed")
        time.sleep(COOLDOWN)
    return None

def main():
    with open(SEASON_IDS_FILE) as f:
        season_map = json.load(f)
    t0 = time.time()
    all_r = {}
    stats = {"total":0,"ok":0,"failed":0,"no_event":0,
             "lu200":0,"sm200":0,"inc200":0,"retries":0}

    for ci,(comp,info) in enumerate(season_map.items()):
        ut=info["ut_id"]; seasons=info["seasons"]
        print(f"\n{'='*60}")
        print(f"[{ci+1}/{len(season_map)}] {comp} (ut_id={ut})")
        print(f"{'='*60}")
        all_r[comp]={}
        for sl,sid in seasons.items():
            stats["total"]+=1
            print(f"  {sl} (id={sid}):")
            fr = run_worker(FIND_WORKER, [ut, sid], timeout=60)
            if not fr or not fr.get("event_id"):
                print("    No event found")
                all_r[comp][sl]={"status":"no_event"}
                stats["no_event"]+=1; continue
            eid=fr["event_id"]; home=fr.get("home","?"); away=fr.get("away","?")
            print(f"    Event {eid}: {home} vs {away}")
            r = fetch_event(eid)
            if r:
                stats["ok"]+=1; stats["retries"]+=r.get("retries",0)
                lu=r.get("lineups",0); sm=r.get("shotmap",0); inc=r.get("incidents",0)
                if lu==200: stats["lu200"]+=1
                if sm==200: stats["sm200"]+=1
                if inc==200: stats["inc200"]+=1
                print(f"    ✅ ok={r['api_ok']}, 403={r['api_403']}, retries={r.get('retries',0)}, LU={lu}, SM={sm}")
                all_r[comp][sl]={"status":"ok","event_id":eid,"home":home,"away":away,
                    "coverage":r.get("coverage",-99),"lineups":lu,"shotmap":sm,
                    "incidents":inc,"graph":r.get("graph",0),
                    "lineups_players":r.get("lineups_players",0),
                    "shotmap_shots":r.get("shotmap_shots",0),
                    "api_ok":r["api_ok"],"api_403":r["api_403"],"retries":r.get("retries",0)}
            else:
                stats["failed"]+=1
                print(f"    ❌ FAILED after {MAX_IP_RETRIES} retries")
                all_r[comp][sl]={"status":"failed","event_id":eid,"home":home,"away":away}
            time.sleep(DELAY_BETWEEN_EVENTS)

    el=(time.time()-t0)/60
    stats["elapsed_min"]=el
    with open(RESULTS_FILE,"w") as f:
        json.dump({"results":all_r,"stats":stats}, f, indent=2, ensure_ascii=False)

    print(f"\n{'='*70}")
    print(f"MASS TEST V2 SUMMARY — Network Capture")
    print(f"{'='*70}")
    print(f"Total: {stats['total']} | OK: {stats['ok']} | Failed: {stats['failed']} | NoEvent: {stats['no_event']}")
    sr=stats['ok']/stats['total']*100 if stats['total'] else 0
    print(f"Success: {sr:.1f}%")
    print(f"LU 200: {stats['lu200']} | SM 200: {stats['sm200']} | INC 200: {stats['inc200']}")
    print(f"Retries: {stats['retries']} | Avg: {stats['retries']/max(stats['ok'],1):.1f}/event")
    print(f"Elapsed: {el:.1f} min")
    print(f"\n{'Competition':<30} {'Tot':>4} {'OK':>3} {'Fl':>3} {'NE':>3} {'LU':>3} {'SM':>3} {'AvgR':>5}")
    print("-"*60)
    for comp,seasons in all_r.items():
        t=len(seasons)
        ok=sum(1 for s in seasons.values() if s.get('status')=='ok')
        fl=sum(1 for s in seasons.values() if s.get('status')=='failed')
        ne=sum(1 for s in seasons.values() if s.get('status')=='no_event')
        lu=sum(1 for s in seasons.values() if s.get('lineups')==200)
        sm=sum(1 for s in seasons.values() if s.get('shotmap')==200)
        ar=sum(s.get('retries',0) for s in seasons.values() if s.get('status')=='ok')/max(ok,1)
        print(f"{comp:<30} {t:>4} {ok:>3} {fl:>3} {ne:>3} {lu:>3} {sm:>3} {ar:>5.1f}")
    print(f"\nResults: {RESULTS_FILE}")

if __name__=="__main__":
    main()
