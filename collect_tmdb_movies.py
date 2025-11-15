"""Utility script for populating the local TMDB-backed catalogue."""

import argparse
import os
import sys
import time
from typing import Dict, List, Optional, Sequence, Tuple

import requests
from tqdm import tqdm

try:
    import streamlit as st  # type: ignore
except Exception:  # pragma: no cover - streamlit not available in CLI usage
    st = None

BASE = "https://api.themoviedb.org/3"
DB_PATH = "movies.sqlite"
DEFAULT_RECENT_YEARS = 10
DEFAULT_YEAR_LIMIT = 150
REQUEST_TIMEOUT = 10
REQUEST_DELAY = 0.25


LANGUAGE_CODE_OVERRIDES = {
    "dk": "da",
    "dk-dk": "da",
    "da-dk": "da",
    "se": "sv",
    "se-se": "sv",
    "sv-se": "sv",
    "no-no": "no",
    "nb": "no",
    "nb-no": "no",
    "nn": "no",
    "nn-no": "no",
    "is-is": "is",
}


def resolve_api_key() -> str:
    """Load the TMDB API key from Streamlit secrets or the environment."""

    if st is not None:
        try:
            return st.secrets["TMDB_API_KEY"]  # type: ignore[index]
        except Exception:  # pragma: no cover - fall back to env var
            pass

    key = os.getenv("TMDB_API_KEY")
    if not key:
        print(
            "❌ Missing TMDB_API_KEY (set in .streamlit/secrets.toml or as an environment variable)."
        )
        sys.exit(1)
    return key


TMDB_KEY = resolve_api_key()


def tmdb_get(path: str, params: Optional[Dict[str, object]] = None, retries: int = 3) -> dict:
    """Fetch JSON from TMDB with retries and a short backoff."""

    payload: Dict[str, object] = {"api_key": TMDB_KEY, "language": "en-US"}
    if params:
        payload.update(params)

    for attempt in range(retries):
        try:
            response = requests.get(
                f"{BASE}{path}", params=payload, timeout=REQUEST_TIMEOUT
            )
            response.raise_for_status()
            return response.json()
        except requests.RequestException:
            if attempt == retries - 1:
                print(f"⚠️  TMDB request failed: {path}")
                return {}
            time.sleep(REQUEST_DELAY * (attempt + 1))
    return {}


def normalise_language_code(code: str) -> Optional[str]:
    """Convert a user-supplied language into the ISO-639-1 code expected by TMDB."""

    if not code:
        return None

    cleaned = code.strip().lower().replace("_", "-")
    if not cleaned:
        return None

    override = LANGUAGE_CODE_OVERRIDES.get(cleaned)
    if override:
        return override

    base = cleaned.split("-", 1)[0]
    base = LANGUAGE_CODE_OVERRIDES.get(base, base)

    if len(base) == 2 and base.isalpha():
        return base

    print(
        f"⚠️  Ignoring invalid language code '{code}'. Use ISO-639-1 codes such as 'is' or 'is-IS'."
    )
    return None


def prepare_spoken_languages(values: Sequence[str]) -> List[str]:
    """Normalise and de-duplicate spoken language filters."""

    codes: List[str] = []
    seen: set[str] = set()
    for value in values:
        code = normalise_language_code(value)
        if not code or code in seen:
            continue
        seen.add(code)
        codes.append(code)
    return codes


def movie_supports_languages(
    detail: dict,
    spoken_languages: Sequence[str],
    fallback_original: Optional[str] = None,
) -> bool:
    """Return True when the TMDB payload matches at least one requested language."""

    if not spoken_languages:
        return True

    spoken = detail.get("spoken_languages") or []
    available = {
        (entry.get("iso_639_1") or "").lower()
        for entry in spoken
        if entry and entry.get("iso_639_1")
    }

    for candidate in (
        detail.get("original_language"),
        fallback_original,
    ):
        if not candidate:
            continue
        code = candidate.lower()
        available.add(code)
        override = LANGUAGE_CODE_OVERRIDES.get(code)
        if override:
            available.add(override)

    normalised = {LANGUAGE_CODE_OVERRIDES.get(code, code) for code in available}
    available.update(normalised)
    return any(language in available for language in spoken_languages)


def create_tables(conn: "sqlite3.Connection") -> None:
    import sqlite3  # Local import to avoid circular dependency in typing.

    conn.executescript(
        """
    CREATE TABLE IF NOT EXISTS movies (
        id INTEGER PRIMARY KEY,
        title TEXT,
        year INTEGER,
        popularity REAL,
        vote_average REAL,
        vote_count INTEGER,
        runtime INTEGER,
        overview TEXT,
        poster_path TEXT,
        collection_id INTEGER,
        collection_name TEXT
    );
    CREATE TABLE IF NOT EXISTS people (
        id INTEGER PRIMARY KEY,
        name TEXT,
        popularity REAL,
        known_for_department TEXT
    );
    CREATE TABLE IF NOT EXISTS movie_people (
        movie_id INTEGER,
        person_id INTEGER,
        role TEXT,
        character TEXT,
        PRIMARY KEY (movie_id, person_id, role)
    );
    CREATE TABLE IF NOT EXISTS companies (
        id INTEGER PRIMARY KEY,
        name TEXT,
        country TEXT
    );
    CREATE TABLE IF NOT EXISTS movie_companies (
        movie_id INTEGER,
        company_id INTEGER,
        PRIMARY KEY (movie_id, company_id)
    );
    CREATE TABLE IF NOT EXISTS movie_genres (
        movie_id INTEGER,
        genre TEXT,
        PRIMARY KEY (movie_id, genre)
    );
    CREATE TABLE IF NOT EXISTS movie_languages (
        movie_id INTEGER,
        language_code TEXT,
        language_name TEXT,
        PRIMARY KEY (movie_id, language_code)
    );
    """
    )
    ensure_movie_columns(conn)
    conn.commit()


def ensure_movie_columns(conn: "sqlite3.Connection") -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(movies)")}
    if "poster_path" not in columns:
        conn.execute("ALTER TABLE movies ADD COLUMN poster_path TEXT")
        conn.commit()


def already_collected_ids(cur: "sqlite3.Cursor") -> Tuple[set, set, set]:
    cur.execute("SELECT id FROM movies")
    movies = {row[0] for row in cur.fetchall()}
    cur.execute("SELECT DISTINCT movie_id FROM movie_genres")
    with_genres = {row[0] for row in cur.fetchall()}
    missing_genres = movies - with_genres
    cur.execute("SELECT DISTINCT movie_id FROM movie_languages")
    with_languages = {row[0] for row in cur.fetchall()}
    missing_languages = movies - with_languages
    return movies, missing_genres, missing_languages


def backfill_spoken_languages(
    conn: "sqlite3.Connection",
    cur: "sqlite3.Cursor",
    missing_language_ids: Sequence[int],
    metadata_language: str,
) -> set[int]:
    """Populate spoken-language metadata for movies collected before the new table."""

    ids = sorted({movie_id for movie_id in missing_language_ids if movie_id})
    if not ids:
        return set()

    print(
        f"🛠️  Backfilling spoken languages for {len(ids)} existing movies so language filters work."
    )

    updated: set[int] = set()
    batch = 0
    for movie_id in tqdm(ids, desc="  Updating languages", unit="movie"):
        detail = fetch_movie_details(movie_id, metadata_language)
        if not detail:
            continue

        insert_languages(cur, movie_id, detail.get("spoken_languages"))
        if detail.get("spoken_languages"):
            updated.add(movie_id)

        batch += 1
        if batch % 25 == 0:
            conn.commit()

    conn.commit()

    if updated:
        print(f"  ✅ Recorded spoken languages for {len(updated)} movies.\n")

    remaining = set(ids) - updated
    if remaining:
        print(
            "  ⚠️  Some movies still lack language data on TMDB; they'll be refreshed if needed.\n"
        )

    return remaining


def fetch_genre_map() -> Dict[str, int]:
    payload = tmdb_get("/genre/movie/list")
    genres = payload.get("genres", [])
    return {genre["name"].lower(): int(genre["id"]) for genre in genres if genre.get("id")}


GENRE_CACHE: Optional[Dict[str, int]] = None
PERSON_CACHE: Dict[Tuple[str, Optional[str]], Optional[int]] = {}


def resolve_genre_ids(names: Sequence[str]) -> List[int]:
    global GENRE_CACHE
    if not names:
        return []
    if GENRE_CACHE is None:
        GENRE_CACHE = fetch_genre_map()
    ids: List[int] = []
    for name in names:
        key = name.strip().lower()
        if not key:
            continue
        genre_id = GENRE_CACHE.get(key)
        if genre_id is None:
            print(f"⚠️  Unknown genre '{name}'.")
            continue
        ids.append(genre_id)
    return ids


def search_person_id(name: str, department: Optional[str]) -> Optional[int]:
    cache_key = (name.lower(), department)
    if cache_key in PERSON_CACHE:
        return PERSON_CACHE[cache_key]

    payload = tmdb_get(
        "/search/person",
        params={"query": name, "include_adult": "false"},
    )
    results = payload.get("results", [])
    if not results:
        print(f"⚠️  Could not find TMDB person for '{name}'.")
        PERSON_CACHE[cache_key] = None
        return None

    if department:
        for person in results:
            if person.get("known_for_department") == department:
                PERSON_CACHE[cache_key] = int(person["id"])
                return PERSON_CACHE[cache_key]

    PERSON_CACHE[cache_key] = int(results[0]["id"])
    return PERSON_CACHE[cache_key]


def resolve_person_ids(names: Sequence[str], department: Optional[str]) -> List[int]:
    ids: List[int] = []
    for name in names:
        name = name.strip()
        if not name:
            continue
        person_id = search_person_id(name, department)
        if person_id is not None:
            ids.append(person_id)
    return ids


def discover_movies(
    *,
    year: Optional[int],
    limit: int,
    cast_ids: Sequence[int],
    crew_ids: Sequence[int],
    genre_ids: Sequence[int],
    metadata_language: str,
    spoken_languages: Sequence[str],
    min_vote_average: Optional[float],
    min_vote_count: Optional[int],
) -> List[dict]:
    movies: List[dict] = []
    if limit <= 0:
        return movies

    page = 1
    total_pages = 1
    language_filter = "|".join(spoken_languages) if spoken_languages else None

    while len(movies) < limit and page <= total_pages:
        params: Dict[str, object] = {
            "sort_by": "popularity.desc",
            "include_adult": "false",
            "page": page,
            "language": metadata_language,
        }
        if year:
            params["primary_release_year"] = year
        if cast_ids:
            params["with_cast"] = ",".join(str(cast_id) for cast_id in cast_ids)
        if crew_ids:
            params["with_crew"] = ",".join(str(crew_id) for crew_id in crew_ids)
        if genre_ids:
            params["with_genres"] = ",".join(str(genre_id) for genre_id in genre_ids)
        if language_filter:
            params["with_spoken_languages"] = language_filter
            params["with_original_language"] = language_filter
        if min_vote_average is not None:
            params["vote_average.gte"] = min_vote_average
        if min_vote_count is not None:
            params["vote_count.gte"] = min_vote_count

        payload = tmdb_get("/discover/movie", params=params)
        results = payload.get("results", [])
        if not results:
            break

        movies.extend(results)
        total_pages = payload.get("total_pages", total_pages)
        page += 1
        time.sleep(REQUEST_DELAY)

    return movies[:limit]


def fetch_movie_details(movie_id: int, metadata_language: str) -> dict:
    payload = tmdb_get(
        f"/movie/{movie_id}",
        params={"append_to_response": "credits", "language": metadata_language},
    )
    time.sleep(REQUEST_DELAY)
    return payload


def insert_movie(cur: "sqlite3.Cursor", m: dict) -> None:
    cur.execute(
        """
        INSERT OR REPLACE INTO movies
        (id,title,year,popularity,vote_average,vote_count,runtime,overview,poster_path,collection_id,collection_name)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """,
        (
            m["id"],
            m.get("title"),
            (m.get("release_date") or "0000")[:4],
            m.get("popularity"),
            m.get("vote_average"),
            m.get("vote_count"),
            m.get("runtime"),
            m.get("overview"),
            m.get("poster_path"),
            (m.get("belongs_to_collection") or {}).get("id")
            if m.get("belongs_to_collection")
            else None,
            (m.get("belongs_to_collection") or {}).get("name")
            if m.get("belongs_to_collection")
            else None,
        ),
    )


def insert_people_and_links(cur: "sqlite3.Cursor", movie_id: int, credits: Optional[dict]) -> None:
    if not credits:
        return

    for cast in credits.get("cast", [])[:10]:
        cur.execute(
            "INSERT OR IGNORE INTO people VALUES (?,?,?,?)",
            (
                cast["id"],
                cast.get("name"),
                cast.get("popularity"),
                "Acting",
            ),
        )
        cur.execute(
            "INSERT OR REPLACE INTO movie_people VALUES (?,?,?,?)",
            (
                movie_id,
                cast["id"],
                "Actor",
                cast.get("character"),
            ),
        )

    for crew in credits.get("crew", []):
        if crew.get("job") == "Director":
            cur.execute(
                "INSERT OR IGNORE INTO people VALUES (?,?,?,?)",
                (
                    crew["id"],
                    crew.get("name"),
                    crew.get("popularity"),
                    "Directing",
                ),
            )
            cur.execute(
                "INSERT OR REPLACE INTO movie_people VALUES (?,?,?,?)",
                (
                    movie_id,
                    crew["id"],
                    "Director",
                    None,
                ),
            )


def insert_companies(cur: "sqlite3.Cursor", movie_id: int, companies: Optional[Sequence[dict]]) -> None:
    for company in companies or []:
        cur.execute(
            "INSERT OR IGNORE INTO companies VALUES (?,?,?)",
            (
                company["id"],
                company.get("name"),
                company.get("origin_country"),
            ),
        )
        cur.execute(
            "INSERT OR REPLACE INTO movie_companies VALUES (?,?)",
            (movie_id, company["id"]),
        )


def insert_genres(cur: "sqlite3.Cursor", movie_id: int, genres: Optional[Sequence[dict]]) -> None:
    for genre in genres or []:
        name = genre.get("name")
        if name:
            cur.execute(
                "INSERT OR IGNORE INTO movie_genres VALUES (?,?)",
                (movie_id, name),
            )


def insert_languages(
    cur: "sqlite3.Cursor", movie_id: int, languages: Optional[Sequence[dict]]
) -> None:
    for entry in languages or []:
        code = (entry.get("iso_639_1") or "").strip().lower()
        if not code:
            continue
        name = (
            entry.get("english_name")
            or entry.get("name")
            or entry.get("iso_639_1")
            or code
        )
        cur.execute(
            "INSERT OR REPLACE INTO movie_languages VALUES (?,?,?)",
            (movie_id, code, name),
        )


def determine_years(args: argparse.Namespace) -> List[int]:
    if args.year:
        unique_years = sorted({year for year in args.year if year}, reverse=True)
        return unique_years

    current_year = time.localtime().tm_year
    start_year = args.from_year if args.from_year else current_year
    end_year = args.to_year if args.to_year else current_year - DEFAULT_RECENT_YEARS + 1

    if start_year < end_year:
        start_year, end_year = end_year, start_year

    return list(range(start_year, end_year - 1, -1))


def describe_filters(args: argparse.Namespace, years: Sequence[int]) -> None:
    parts = [f"Years: {', '.join(str(year) for year in years)}"]
    if args.genre:
        parts.append(f"Genres: {', '.join(args.genre)}")
    if args.actor:
        parts.append(f"Actors: {', '.join(args.actor)}")
    if args.director:
        parts.append(f"Directors: {', '.join(args.director)}")
    if getattr(args, "metadata_language", None):
        parts.append(f"Metadata locale: {args.metadata_language}")
    if getattr(args, "spoken_language", None):
        parts.append(
            "Languages: " + ", ".join(sorted(args.spoken_language))
        )
    if args.min_rating is not None:
        parts.append(f"Min rating: {args.min_rating}")
    if args.min_vote_count is not None:
        parts.append(f"Min vote count: {args.min_vote_count}")
    print("🎯 Filters → " + " | ".join(parts))


def parse_args() -> argparse.Namespace:
    examples = """Examples:
      Collect popular releases from 2022 only:
        python collect_tmdb_movies.py --year 2022

      Pull 60 comedies featuring Emma Stone or Ryan Gosling:
        python collect_tmdb_movies.py --genre Comedy --actor "Emma Stone" --actor "Ryan Gosling" --number 60

      Refresh thrillers directed by Denis Villeneuve since 2016:
        python collect_tmdb_movies.py --director "Denis Villeneuve" --genre Thriller --from-year 2024 --to-year 2016

      Gather Nordic-language dramas released since 2015:
        python collect_tmdb_movies.py --genre Drama --language is-IS --language da-DK --language sv-SE --language no-NO --from-year 2024 --to-year 2015
    """

    parser = argparse.ArgumentParser(
        description="Collect movie metadata from TMDB and populate the local SQLite cache.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=examples,
    )
    parser.add_argument(
        "--year",
        "-y",
        action="append",
        type=int,
        help="Specific release year to pull (repeat for multiple years).",
    )
    parser.add_argument(
        "--from-year",
        type=int,
        help="Latest release year to include when building a range (defaults to current year).",
    )
    parser.add_argument(
        "--to-year",
        type=int,
        help="Oldest release year to include when building a range (defaults to current year minus 9).",
    )
    parser.add_argument(
        "--number",
        "-n",
        type=int,
        help="Maximum number of movies to fetch across all years (defaults to 150 per year).",
    )
    parser.add_argument(
        "--genre",
        "-g",
        action="append",
        help="Filter by genre name (matches TMDB names; repeat to require multiple genres).",
    )
    parser.add_argument(
        "--actor",
        "-a",
        action="append",
        help="Only include titles featuring the specified actor (repeat for multiple actors).",
    )
    parser.add_argument(
        "--director",
        "-d",
        action="append",
        help="Only include titles directed by the specified filmmaker (repeat for multiple directors).",
    )
    parser.add_argument(
        "--min-rating",
        type=float,
        help="Minimum TMDB average vote to include (e.g. 7.5).",
    )
    parser.add_argument(
        "--min-vote-count",
        type=int,
        help="Minimum TMDB vote count to include (helps avoid obscure titles).",
    )
    parser.add_argument(
        "--metadata-language",
        default="en-US",
        help="Locale code used for translated metadata responses (default: en-US).",
    )
    parser.add_argument(
        "--language",
        "-L",
        dest="spoken_language",
        action="append",
        help=(
            "Restrict movies to those featuring the given spoken or original language. "
            "Accepts ISO-639-1 codes such as 'is' or locale values like 'is-IS'. "
            "Repeat the flag to allow multiple languages."
        ),
    )
    return parser.parse_args()


def main() -> None:
    import sqlite3

    args = parse_args()
    raw_spoken = args.spoken_language or []
    spoken_languages = prepare_spoken_languages(raw_spoken)
    if raw_spoken and not spoken_languages:
        print(
            "⚠️  No valid language codes provided; proceeding without a spoken-language filter."
        )
    args.spoken_language = spoken_languages

    years = determine_years(args)
    if not years:
        print("⚠️  No years to query. Specify --year or a year range.")
        return

    describe_filters(args, years)

    genre_ids = resolve_genre_ids(args.genre or [])
    cast_ids = resolve_person_ids(args.actor or [], department="Acting")
    crew_ids = resolve_person_ids(args.director or [], department="Directing")

    if args.number is not None and args.number <= 0:
        print("⚠️  --number must be a positive integer. Falling back to the default limit.")
        remaining = None
    else:
        remaining = args.number

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    create_tables(conn)

    collected, missing_genres, missing_languages = already_collected_ids(cur)
    print(f"🗂  {len(collected)} movies already in database. Will skip those.\n")

    if missing_languages:
        missing_languages = backfill_spoken_languages(
            conn, cur, missing_languages, args.metadata_language
        )

    if missing_genres:
        print(
            f"ℹ️  {len(missing_genres)} movies are missing genre data and will be refreshed.\n"
        )
    if missing_languages:
        print(
            f"ℹ️  {len(missing_languages)} movies are missing spoken language data and will be refreshed.\n"
        )

    for year in years:
        if remaining is not None and remaining <= 0:
            break

        year_limit = (
            min(remaining, DEFAULT_YEAR_LIMIT) if remaining is not None else DEFAULT_YEAR_LIMIT
        )
        print(f"📅 Collecting {year} (limit {year_limit})...")

        candidates = discover_movies(
            year=year,
            limit=year_limit,
            cast_ids=cast_ids,
            crew_ids=crew_ids,
            genre_ids=genre_ids,
            metadata_language=args.metadata_language,
            spoken_languages=args.spoken_language,
            min_vote_average=args.min_rating,
            min_vote_count=args.min_vote_count,
        )

        to_fetch = [
            movie
            for movie in candidates
            if movie["id"] not in collected
            or movie["id"] in missing_genres
            or movie["id"] in missing_languages
        ]

        if not to_fetch:
            print(f"  ✅ No new titles for {year} with the current filters.")
            continue

        for movie in tqdm(to_fetch, desc=f"  Fetching {year}", unit="movie"):
            detail = fetch_movie_details(movie["id"], args.metadata_language)
            if not detail:
                continue
            if args.spoken_language and not movie_supports_languages(
                detail, args.spoken_language, movie.get("original_language")
            ):
                title = detail.get("title") or movie.get("title") or movie.get("name")
                print(
                    f"   ↪ Skipping {title or movie['id']} — language metadata does not match the requested filter."
                )
                continue
            insert_movie(cur, detail)
            insert_people_and_links(cur, movie["id"], detail.get("credits"))
            insert_companies(cur, movie["id"], detail.get("production_companies"))
            insert_genres(cur, movie["id"], detail.get("genres"))
            insert_languages(cur, movie["id"], detail.get("spoken_languages"))
            conn.commit()
            collected.add(movie["id"])
            missing_genres.discard(movie["id"])
            missing_languages.discard(movie["id"])
            if remaining is not None:
                remaining -= 1
                if remaining <= 0:
                    break

    conn.close()
    print("\n✅ Finished collecting TMDB data. Safe to rerun anytime — it resumes automatically.")


if __name__ == "__main__":
    main()
