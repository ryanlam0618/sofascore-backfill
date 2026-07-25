#!/usr/bin/env python3
"""Rebuild SofaScore seasons from the 24-competition season URL map."""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any

import mysql.connector


ROOT = Path(__file__).resolve().parent
DEFAULT_ENV_PATH = ROOT.parent / ".env"
DEFAULT_SEASONS_PATH = ROOT / "data" / "season_urls_24_competitions.json"

# The production competition table was missing this official 24-competition target.
# Keep the insertion narrow and deterministic so seasons for ut_id=668 can satisfy FK.
MISSING_COMPETITIONS = {
    668: {
        "competition_id": 668,
        "name": "AFC Champions League Two",
        "short_name": None,
        "category_id": 1467,
        "type": "international",
        "country_code": None,
        "season_mode": "international",
    }
}


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def parse_year_label(label: str) -> tuple[int | None, int | None]:
    value = str(label).strip()
    if not value:
        return None, None

    four_digit = re.fullmatch(r"(\d{4})", value)
    if four_digit:
        year = int(four_digit.group(1))
        return year, year

    short_range = re.fullmatch(r"(\d{2})/(\d{2})", value)
    if short_range:
        start_two = int(short_range.group(1))
        end_two = int(short_range.group(2))
        start_year = 2000 + start_two if start_two <= 49 else 1900 + start_two
        end_year = 2000 + end_two if end_two <= 49 else 1900 + end_two
        if end_year < start_year:
            end_year += 100
        return start_year, end_year

    long_range = re.fullmatch(r"(\d{4})/(\d{2}|\d{4})", value)
    if long_range:
        start_year = int(long_range.group(1))
        end_part = long_range.group(2)
        if len(end_part) == 2:
            century = start_year // 100 * 100
            end_year = century + int(end_part)
            if end_year < start_year:
                end_year += 100
        else:
            end_year = int(end_part)
        return start_year, end_year

    raise ValueError(f"Unsupported season year label: {label!r}")


def connect() -> mysql.connector.MySQLConnection:
    password = os.environ.get("MYSQL_PASSWORD")
    if not password:
        raise RuntimeError("MYSQL_PASSWORD is not set; expected it in .env")
    return mysql.connector.connect(
        host=os.environ.get("MYSQL_HOST", "127.0.0.1"),
        port=int(os.environ.get("MYSQL_PORT", "3306")),
        user=os.environ.get("MYSQL_USER", "root"),
        password=password,
        database=os.environ.get("MYSQL_DATABASE", "appdb"),
        autocommit=False,
    )


def ensure_missing_competition(cur: Any, ut_id: int) -> bool:
    spec = MISSING_COMPETITIONS.get(ut_id)
    if not spec:
        return False
    cur.execute("SELECT competition_id FROM competitions WHERE ut_id = %s", (ut_id,))
    if cur.fetchone():
        return False
    cur.execute(
        """
        INSERT INTO competitions
            (competition_id, name, short_name, category_id, ut_id, type, country_code, season_mode)
        VALUES
            (%s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            name = VALUES(name),
            short_name = VALUES(short_name),
            category_id = VALUES(category_id),
            ut_id = VALUES(ut_id),
            type = VALUES(type),
            country_code = VALUES(country_code),
            season_mode = VALUES(season_mode)
        """,
        (
            spec["competition_id"],
            spec["name"],
            spec["short_name"],
            spec["category_id"],
            ut_id,
            spec["type"],
            spec["country_code"],
            spec["season_mode"],
        ),
    )
    return True


def rebuild(seasons_path: Path) -> dict[str, Any]:
    with seasons_path.open() as fh:
        payload = json.load(fh)

    competitions = payload.get("competitions", [])
    conn = connect()
    try:
        cur = conn.cursor(dictionary=True)
        inserted_competitions: list[str] = []
        counts: dict[str, int] = {}
        upserted = 0
        missing: list[dict[str, Any]] = []

        for comp in competitions:
            ut_id = int(comp["ut_id"])
            if ensure_missing_competition(cur, ut_id):
                inserted_competitions.append(comp["name"])

            cur.execute(
                "SELECT competition_id, name FROM competitions WHERE ut_id = %s",
                (ut_id,),
            )
            db_comp = cur.fetchone()
            if not db_comp:
                missing.append({"name": comp["name"], "ut_id": ut_id})
                continue

            comp_name = db_comp["name"] or comp["name"]
            for season in comp.get("seasons", []):
                year_label = str(season["year"]).strip()
                year_start, year_end = parse_year_label(year_label)
                cur.execute(
                    """
                    INSERT INTO seasons
                        (season_id, competition_id, year_label, year_start, year_end, start_date, end_date, is_current)
                    VALUES
                        (%s, %s, %s, %s, %s, NULL, NULL, %s)
                    ON DUPLICATE KEY UPDATE
                        competition_id = VALUES(competition_id),
                        year_label = VALUES(year_label),
                        year_start = VALUES(year_start),
                        year_end = VALUES(year_end),
                        start_date = VALUES(start_date),
                        end_date = VALUES(end_date),
                        is_current = VALUES(is_current)
                    """,
                    (
                        int(season["id"]),
                        int(db_comp["competition_id"]),
                        year_label,
                        year_start,
                        year_end,
                        1 if season is comp.get("seasons", [None])[0] else 0,
                    ),
                )
                upserted += 1
                counts[comp_name] = counts.get(comp_name, 0) + 1

        if missing:
            conn.rollback()
            raise RuntimeError(f"Missing competitions for ut_ids: {missing}")

        conn.commit()
        return {
            "upserted": upserted,
            "seasons_per_competition": counts,
            "inserted_competitions": inserted_competitions,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", type=Path, default=DEFAULT_ENV_PATH)
    parser.add_argument("--seasons", type=Path, default=DEFAULT_SEASONS_PATH)
    args = parser.parse_args()

    load_env(args.env)
    result = rebuild(args.seasons)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
