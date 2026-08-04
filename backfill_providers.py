"""Backfill streaming provider data for all existing movies in the database.

Usage:
    python backfill_providers.py [--regions FI DK IS] [--batch-size 25]

This script is safe to rerun — it skips movies that already have provider rows.
Estimated time: ~22 minutes for 5,315 movies at the default 0.25 s delay.
"""

import argparse
import sqlite3
import sys
import time
from typing import List

from tqdm import tqdm

from collect_tmdb_movies import (
    DB_PATH,
    REQUEST_DELAY,
    create_tables,
    fetch_watch_providers,
    insert_providers,
    resolve_api_key,
)
from app_config import REGIONS

ALL_REGIONS = list(REGIONS.values())  # ['FI', 'DK', 'IS']


def movies_missing_providers(cur: sqlite3.Cursor, regions: List[str]) -> List[int]:
    """Return movie IDs that have no provider rows for ANY of the given regions."""
    placeholders = ",".join("?" * len(regions))
    cur.execute(
        f"""
        SELECT id FROM movies
        WHERE id NOT IN (
            SELECT DISTINCT movie_id FROM movie_providers
            WHERE region IN ({placeholders})
        )
        ORDER BY id
        """,
        regions,
    )
    return [row[0] for row in cur.fetchall()]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill streaming provider data for existing movies.",
    )
    parser.add_argument(
        "--regions",
        nargs="+",
        default=ALL_REGIONS,
        metavar="CODE",
        help=f"Region codes to fetch (default: {' '.join(ALL_REGIONS)}).",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=25,
        metavar="N",
        help="Commit to database every N movies (default: 25).",
    )
    return parser.parse_args()


def main() -> None:
    # Validate the API key early so we fail fast.
    try:
        resolve_api_key()
    except SystemExit:
        sys.exit(1)

    args = parse_args()
    regions = [r.upper() for r in args.regions]
    unknown = [r for r in regions if r not in ALL_REGIONS]
    if unknown:
        print(f"⚠️  Unknown region(s): {', '.join(unknown)}. Valid: {', '.join(ALL_REGIONS)}")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    create_tables(conn)  # ensures movie_providers table exists

    movie_ids = movies_missing_providers(cur, regions)
    total = len(movie_ids)

    if total == 0:
        print("[DONE] All movies already have provider data. Nothing to do.")
        conn.close()
        return

    print(f"[INFO] {total} movies need provider data for regions: {', '.join(regions)}")
    print(f"[INFO] Estimated time: ~{total * REQUEST_DELAY / 60:.0f} minutes\n")

    batch = 0
    for movie_id in tqdm(movie_ids, desc="Fetching providers", unit="movie"):
        providers_by_region = fetch_watch_providers(movie_id)
        insert_providers(cur, movie_id, providers_by_region, regions)

        batch += 1
        if batch % args.batch_size == 0:
            conn.commit()

    conn.commit()
    conn.close()

    # Summary
    conn2 = sqlite3.connect(DB_PATH)
    cur2 = conn2.cursor()
    cur2.execute(
        "SELECT region, COUNT(DISTINCT movie_id) FROM movie_providers GROUP BY region ORDER BY region"
    )
    rows = cur2.fetchall()
    conn2.close()

    print("[DONE] Backfill complete. Provider coverage by region:")
    for region, count in rows:
        print(f"   {region}: {count} movies")


if __name__ == "__main__":
    main()
