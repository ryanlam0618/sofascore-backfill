#!/usr/bin/env python3
"""Fetch SofaScore API data for a match and inspect available fields."""
import json, sys
from playwright.sync_api import sync_playwright

MATCH_ID = 14023966
BASE = "https://api.sofascore.com/api/v1/event"

def fetch_all():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context()
        page = ctx.new_page()
        
        endpoints = {
            'event': f'{BASE}/{MATCH_ID}',
            'lineups': f'{BASE}/{MATCH_ID}/lineups',
            'statistics': f'{BASE}/{MATCH_ID}/statistics',
            'incidents': f'{BASE}/{MATCH_ID}/incidents',
            'shotmap': f'{BASE}/{MATCH_ID}/shotmap',
            'graph': f'{BASE}/{MATCH_ID}/graph',
        }
        
        results = {}
        for name, url in endpoints.items():
            resp = page.goto(url, timeout=30000)
            if resp and resp.ok:
                data = resp.json()
                results[name] = data
                print(f'✅ {name}: {len(json.dumps(data))} bytes')
            else:
                print(f'❌ {name}: status={resp.status if resp else "N/A"}')
        
        browser.close()
    return results

def analyze(results):
    print('\n' + '='*60)
    print('STRUCTURAL ANALYSIS')
    print('='*60)
    
    # 1. EVENT - main match data
    if 'event' in results:
        event = results['event'].get('event', {})
        print(f'\n--- EVENT ---')
        print(f'Keys: {list(event.keys())[:30]}')
        # Check for homeTeam/awayTeam details
        ht = event.get('homeTeam', {})
        at = event.get('awayTeam', {})
        print(f'Home team keys: {list(ht.keys())[:20]}')
        # Check referee
        ref = event.get('referee', {})
        print(f'Referee keys: {list(ref.keys())[:10]}')
        # Check venue
        venue = event.get('venue', {})
        print(f'Venue keys: {list(venue.keys())[:10]}')
        # Check status
        status = event.get('status', {})
        print(f'Status: {status}')
        # Scores
        print(f'Home score: {event.get("homeScore", {})}')
        print(f'Away score: {event.get("awayScore", {})}')
        print(f'Winner: {event.get("winnerCode")}')
        # roundInfo
        ri = event.get('roundInfo', {})
        print(f'Round: {ri}')
        # Attendance
        print(f'Attendance: {event.get("attendance")}')
    
    # 2. LINEUPS - player stats
    if 'lineups' in results:
        lu = results['lineups']
        print(f'\n--- LINEUPS ---')
        print(f'Keys: {list(lu.keys())}')
        
        home_lineup = lu.get('home', {})
        away_lineup = lu.get('away', {})
        
        for side, lineup in [('home', home_lineup), ('away', away_lineup)]:
            players = lineup.get('players', [])
            if players:
                p0 = players[0]
                print(f'\n{side} ({len(players)} players)')
                print(f'Player keys (all): {list(p0.keys())}')
                
                # Show nested structures
                if 'player' in p0:
                    print(f'  player keys: {list(p0["player"].keys())[:20]}')
                
                # Statistics
                if 'statistics' in p0:
                    print(f'  statistics type: {type(p0["statistics"]).__name__}')
                    if isinstance(p0['statistics'], dict):
                        print(f'  statistics keys ({len(p0["statistics"])}):')
                        for k in list(p0['statistics'].keys())[:50]:
                            print(f'    - {k} = {p0["statistics"][k]}')
                
                # Check for substitutes
                subs = [p for p in players if p.get('substitute')]
                if subs:
                    print(f'  Substitutes: {len(subs)}')
                    s0 = subs[0]
                    if 'statistics' in s0:
                        print(f'  Sub stats keys ({len(s0["statistics"])}): {list(s0["statistics"].keys())[:50]}')
            else:
                print(f'\n{side}: No players')
            
            # Check formation
            formation = lineup.get('formation')
            coach = lineup.get('coach', {})
            print(f'  Formation: {formation}')
            print(f'  Coach: {coach.get("name")}')
    
    # 3. STATISTICS
    if 'statistics' in results:
        stats = results['statistics']
        print(f'\n--- STATISTICS ---')
        print(f'Keys: {list(stats.keys())}')
        if 'statistics' in stats:
            stat_groups = stats['statistics']
            if isinstance(stat_groups, list) and stat_groups:
                print(f'A total of {len(stat_groups)} stat groups:')
                for sg in stat_groups[:5]:
                    group = sg.get('groupName', '?')
                    items = sg.get('statisticsItems', [])
                    print(f'  Group: {group} ({len(items)} items)')
                    for item in items[:3]:
                        print(f'    {item.get("name")}: home={item.get("home")} away={item.get("away")}')
    
    # 4. INCIDENTS
    if 'incidents' in results:
        inc = results['incidents']
        print(f'\n--- INCIDENTS ---')
        if 'incidents' in inc and isinstance(inc['incidents'], list):
            print(f'Total: {len(inc["incidents"])} incidents')
            # Sample
            for i in inc['incidents'][:3]:
                print(f'  {i.get("incidentType")} min={i.get("time")} player={i.get("player",{}).get("name","?")}')
            # Show keys
            if inc['incidents']:
                i0 = inc['incidents'][0]
                print(f'Incident keys: {list(i0.keys())}')
    
    # 5. SHOTMAP
    if 'shotmap' in results:
        sm = results['shotmap']
        print(f'\n--- SHOTMAP ---')
        print(f'Keys: {list(sm.keys())}')
        if 'shotmap' in sm:
            shots = sm['shotmap']
            print(f'Total shots: {len(shots)}')
            if shots:
                s0 = shots[0]
                print(f'Shot keys: {list(s0.keys())}')
                # Check for coordinates, xGOT, goalkeeper
                for key in ['goalMouthLocation', 'goalMouthY', 'goalMouthZ', 'expectedGoalsOnTarget', 'xgot', 'xg', 'playerCoordinates', 'goalkeeper', 'bodyPart', 'shotType', 'situation']:
                    val = s0.get(key, 'N/A')
                    print(f'  {key}: {val}')
                # Check nested player
                if 'player' in s0:
                    print(f'  player: {json.dumps(s0["player"], default=str)[:200]}')
    
    # 6. GRAPH
    if 'graph' in results:
        gr = results['graph']
        print(f'\n--- GRAPH ---')
        print(f'Keys: {list(gr.keys())}')
        if 'graphPoints' in gr:
            pts = gr['graphPoints']
            print(f'Graph points: {len(pts)}')
            if pts:
                print(f'Point keys: {list(pts[0].keys())[:10]}')

if __name__ == '__main__':
    results = fetch_all()
    analyze(results)
    
    # Also try the player-statistics endpoint
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context()
        page = ctx.new_page()
        
        extra_endpoints = [
            f'{BASE}/{MATCH_ID}/player-statistics',
            f'{BASE}/{MATCH_ID}/momentum',
            f'{BASE}/{MATCH_ID}/h2h',
            f'{BASE}/{MATCH_ID}/tv',
        ]
        
        print(f'\n--- EXTRA ENDPOINTS ---')
        for url in extra_endpoints:
            resp = page.goto(url, timeout=30000)
            if resp and resp.ok:
                data = resp.json()
                keys = list(data.keys()) if isinstance(data, dict) else 'list'
                size = len(json.dumps(data))
                print(f'✅ {url.split("/")[-1]}: {size} bytes, keys={keys[:15]}')
            else:
                print(f'❌ {url.split("/")[-1]}: status={resp.status if resp else "N/A"}')
        
        browser.close()
