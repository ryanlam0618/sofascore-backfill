#!/usr/bin/env python3
"""Discover all competitions' 2015-16 to 2024-25 season IDs and one event per season."""

import json
import time
from cloakbrowser import launch

# Load competitions config
import yaml
with open("competitions_10y.yaml") as f:
    config = yaml.safe_load(f)

comps = config["competitions"]
print(f"Loaded {len(comps)} competitions")

browser = launch(headless=True, humanize=True)
page = browser.new_page()

results = {}

for comp in comps:
    name = comp["name"]
    ut_id = comp["ut_id"]
    season_mode = comp.get("season_mode", "national")
    results[name] = {"ut_id": ut_id, "seasons": {}}
    
    print(f"\n=== {name} (ut_id={ut_id}) ===")
    
    # Get seasons
    try:
        resp = page.goto(f"https://www.sofascore.com/api/v1/unique-tournament/{ut_id}/seasons", timeout=30000)
        status = resp.status if resp else 'N/A'
        if status != 200:
            print(f"  Seasons endpoint: {status} ⚠️")
            continue
        body = page.evaluate("() => document.body.innerText")
        data = json.loads(body)
        seasons = data.get("seasons", [])
        
        # Filter 15/16 through 24/25
        for s in seasons:
            year = s.get("year", "")
            sid = s.get("id")
            # Match formats like "15/16", "2015/2016", "2015", "15"
            target_years = ["15/16","16/17","17/18","18/19","19/20","20/21","21/22","22/23","23/24","24/25","25/26",
                            "2015/2016","2016/2017","2017/2018","2018/2019","2019/2020","2020/2021","2021/2022","2022/2023","2023/2024","2024/2025","2025/2026",
                            "2015","2016","2017","2018","2019","2020","2021","2022","2023","2024","2025"]
            if year in target_years:
                results[name]["seasons"][year] = sid
                print(f"  {year} -> {sid}")
        time.sleep(0.5)
    except Exception as e:
        print(f"  Error: {e}")

browser.close()

# Save
with open("data/cloak_test_season_ids.json", "w") as f:
    json.dump(results, f, indent=2, ensure_ascii=False)

print(f"\nSaved to data/cloak_test_season_ids.json")
print(f"Total competitions: {len(results)}")
for name, info in results.items():
    print(f"  {name}: {len(info['seasons'])} seasons found")
