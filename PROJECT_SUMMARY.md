# ⚽ SofaScore Backfill — Project Summary

**Goal**: build a 10-year historical football data warehouse — crawl SofaScore's historical match data into a local MySQL database (`appdb`) for analytics, model training, and odds research, without relying on the live API.

---

## 📦 Scope

- **Competitions**: 24 (Big Five leagues, UCL / UEL, domestic cups, Asian leagues, etc.)
- **Seasons**: 2015/16 → 2024/25 (10 years)
- **Scale**: tens of thousands of matches × 5 endpoints each

## 🔌 Data fetched per match (5 endpoints)

1. `/event` — score, venue, referee, attendance, metadata
2. `/lineups` — starting XI and substitutes
3. `/statistics` — possession, shots, corners, team stats
4. `/incidents` — goals, cards, substitutions timeline
5. `/shotmap` — per-shot xG and coordinates 🆕

(Odds endpoint currently paused.)

## 🧱 Engine: Gen4 fp v2 (production)

SofaScore is protected by Cloudflare/Varnish + IP-reputation blocking. The breakthrough:

- **21 healthy proxy IPs** (dynamic scoring via `ip_health_score.py`)
- **curl_cffi with chrome124 browser fingerprint**
- Deterministic hash routing: each match is pinned to a fixed IP — stable and reproducible
- Success rate **~99.9%**, zero bans observed ✅

History: Gen2/Gen3 (~87% success, frequent 403s) — retired.

## 📊 Status (2026-09-13, Tranche v2 in flight)

- 25/26 season complete (Stage 4, 606K rows)
- Older-season backfill (from 15/16 onward) in mass production: Big Five 15/16–16/17 done at ~100%
- Latest evidence: 3,250 matches, zero HTTP 403, 99.88% success

## 🗃️ Storage

- MySQL `appdb`: `matches`, `match_lineups`, `match_statistics`, `match_incidents`, `match_shotmap`, `fetch_logs`, etc. (23 tables)
- Audit trail: per-match JSONL evidence + `fetch_logs` table

---

Full operational details: see `README.md`.
