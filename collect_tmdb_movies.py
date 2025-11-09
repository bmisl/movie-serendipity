# collect_tmdb_movies.py
import os, sys, time, sqlite3, requests
from tqdm import tqdm

# Try to load key from Streamlit secrets if available
try:
    import streamlit as st
    TMDB_KEY = st.secrets["TMDB_API_KEY"]
except Exception:
    TMDB_KEY = os.getenv("TMDB_API_KEY")

if not TMDB_KEY:
    print("❌ Missing TMDB_API_KEY (set in .streamlit/secrets.toml or as environment variable).")
    sys.exit(1)

BASE = "https://api.themoviedb.org/3"
DB_PATH = "movies.sqlite"

def get_json(url, retries=3, delay=0.25):
    """Fetch JSON data with retries."""
    for attempt in range(retries):
        r = requests.get(url, params={"api_key": TMDB_KEY})
        if r.status_code == 200:
            return r.json()
        time.sleep(1 + attempt)
    print(f"⚠️ Failed: {url}")
    return {}


def create_tables(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS movies (
        id INTEGER PRIMARY KEY,
        title TEXT,
        year INTEGER,
        popularity REAL,
        vote_average REAL,
        vote_count INTEGER,
        runtime INTEGER,
        overview TEXT,
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
    """)
    conn.commit()

def discover_movies_by_year(year, limit=150):
    """Get ~150 popular movies from TMDB for a given year."""
    movies = []
    for page in range(1, 9):  # 8 pages × 20 = 160 movies
        url = f"{BASE}/discover/movie?sort_by=popularity.desc&primary_release_year={year}&page={page}"
        data = get_json(url)
        results = data.get("results", [])
        movies.extend(results)
        if len(movies) >= limit:
            break
        time.sleep(0.25)
    return movies[:limit]

def fetch_movie_details(movie_id):
    """Fetch detailed info with credits."""
    detail = get_json(f"{BASE}/movie/{movie_id}?append_to_response=credits")
    time.sleep(0.25)
    return detail

def insert_movie(cur, m):
    cur.execute("""
        INSERT OR REPLACE INTO movies 
        (id,title,year,popularity,vote_average,vote_count,runtime,overview,collection_id,collection_name)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """, (
        m["id"], m["title"], m.get("release_date", "0000")[:4],
        m.get("popularity"), m.get("vote_average"), m.get("vote_count"),
        m.get("runtime"), m.get("overview"),
        (m["belongs_to_collection"] or {}).get("id") if m.get("belongs_to_collection") else None,
        (m["belongs_to_collection"] or {}).get("name") if m.get("belongs_to_collection") else None
    ))

def insert_people_and_links(cur, movie_id, credits):
    if not credits:
        return
    # Actors (top 10)
    for cast in credits.get("cast", [])[:10]:
        cur.execute("INSERT OR IGNORE INTO people VALUES (?,?,?,?)",
                    (cast["id"], cast["name"], cast.get("popularity"), "Acting"))
        cur.execute("INSERT OR REPLACE INTO movie_people VALUES (?,?,?,?)",
                    (movie_id, cast["id"], "Actor", cast.get("character")))
    # Directors
    for crew in credits.get("crew", []):
        if crew.get("job") == "Director":
            cur.execute("INSERT OR IGNORE INTO people VALUES (?,?,?,?)",
                        (crew["id"], crew["name"], crew.get("popularity"), "Directing"))
            cur.execute("INSERT OR REPLACE INTO movie_people VALUES (?,?,?,?)",
                        (movie_id, crew["id"], "Director", None))

def insert_companies(cur, movie_id, companies):
    for c in companies or []:
        cur.execute("INSERT OR IGNORE INTO companies VALUES (?,?,?)",
                    (c["id"], c["name"], c.get("origin_country")))
        cur.execute("INSERT OR REPLACE INTO movie_companies VALUES (?,?)",
                    (movie_id, c["id"]))

def already_collected_ids(cur):
    cur.execute("SELECT id FROM movies")
    return {row[0] for row in cur.fetchall()}

def main():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    create_tables(conn)

    collected = already_collected_ids(cur)
    print(f"🗂  {len(collected)} movies already in database. Will skip those.\n")

    for year in range(2024, 2014, -1):
        print(f"📅 Collecting {year}...")
        movies = discover_movies_by_year(year)
        to_fetch = [m for m in movies if m["id"] not in collected]

        if not to_fetch:
            print(f"  ✅ All {year} movies already collected.")
            continue

        for m in tqdm(to_fetch, desc=f"  Fetching {year}", unit="movie"):
            detail = fetch_movie_details(m["id"])
            if not detail:
                continue
            insert_movie(cur, detail)
            insert_people_and_links(cur, m["id"], detail.get("credits"))
            insert_companies(cur, m["id"], detail.get("production_companies"))
            conn.commit()

    conn.close()
    print("\n✅ Finished collecting TMDB data. Safe to rerun anytime — it resumes automatically.")

if __name__ == "__main__":
    main()
