# movie_dashboard.py
import streamlit as st
import sqlite3
import pandas as pd
import requests
import time

# --- CONFIG ---
st.set_page_config(page_title="🎬 Movie Database Dashboard", layout="wide")
DB_PATH = "movies.sqlite"
TMDB_KEY = st.secrets["TMDB_API_KEY"]
BASE = "https://api.themoviedb.org/3"


# --- DATABASE HELPERS ---
def get_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def get_df(query, params=()):
    conn = get_conn()
    df = pd.read_sql_query(query, conn, params=params)
    conn.close()
    return df


# --- DATA FETCH ---
def get_json(url, retries=3, delay=0.25):
    for attempt in range(retries):
        r = requests.get(url, params={"api_key": TMDB_KEY})
        if r.status_code == 200:
            return r.json()
        time.sleep(1 + attempt)
    st.warning(f"⚠️ Failed: {url}")
    return {}

def discover_movies(year, pages=3, limit=60):
    movies = []
    for page in range(1, pages + 1):
        url = f"{BASE}/discover/movie?sort_by=popularity.desc&primary_release_year={year}&page={page}"
        data = get_json(url)
        results = data.get("results", [])
        movies.extend(results)
        if len(movies) >= limit:
            break
        time.sleep(0.25)
    return movies[:limit]

def fetch_movie_detail(movie_id):
    url = f"{BASE}/movie/{movie_id}?append_to_response=credits"
    detail = get_json(url)
    time.sleep(0.25)
    return detail


# --- INSERT HELPERS ---
def insert_movie(conn, m):
    conn.execute("""
        INSERT OR IGNORE INTO movies 
        (id,title,year,popularity,vote_average,vote_count,runtime,overview,collection_id,collection_name)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """, (
        m["id"], m["title"], m.get("release_date", "0000")[:4],
        m.get("popularity"), m.get("vote_average"), m.get("vote_count"),
        m.get("runtime"), m.get("overview"),
        (m["belongs_to_collection"] or {}).get("id") if m.get("belongs_to_collection") else None,
        (m["belongs_to_collection"] or {}).get("name") if m.get("belongs_to_collection") else None
    ))

def insert_people(conn, movie_id, credits):
    if not credits:
        return
    for cast in credits.get("cast", [])[:10]:
        conn.execute("INSERT OR IGNORE INTO people VALUES (?,?,?,?)",
                     (cast["id"], cast["name"], cast.get("popularity"), "Acting"))
        conn.execute("INSERT OR REPLACE INTO movie_people VALUES (?,?,?,?)",
                     (movie_id, cast["id"], "Actor", cast.get("character")))
    for crew in credits.get("crew", []):
        if crew.get("job") == "Director":
            conn.execute("INSERT OR IGNORE INTO people VALUES (?,?,?,?)",
                         (crew["id"], crew["name"], crew.get("popularity"), "Directing"))
            conn.execute("INSERT OR REPLACE INTO movie_people VALUES (?,?,?,?)",
                         (movie_id, crew["id"], "Director", None))

def insert_genres(conn, movie_id, genres):
    if not genres:
        return
    conn.execute("""
        CREATE TABLE IF NOT EXISTS movie_genres (
            movie_id INTEGER,
            genre TEXT,
            PRIMARY KEY (movie_id, genre)
        )
    """)
    for g in genres:
        conn.execute("INSERT OR IGNORE INTO movie_genres VALUES (?, ?)", (movie_id, g["name"]))


# --- UI ---
st.title("🎬 Local Movie Database Explorer")

tab1, tab2 = st.tabs(["📊 Browse Database", "🔄 Update Database"])

# --------------------------------------------------
# TAB 1: Browse
# --------------------------------------------------
with tab1:
    st.header("Explore your collected movies")

    years = get_df("SELECT DISTINCT year FROM movies ORDER BY year DESC")["year"].tolist()
    selected_year = st.selectbox("Year", ["All"] + years)

    actors = get_df("SELECT DISTINCT name FROM people WHERE known_for_department='Acting' ORDER BY name LIMIT 100")["name"].tolist()
    selected_actor = st.selectbox("Actor", ["All"] + actors)

    directors = get_df("SELECT DISTINCT name FROM people WHERE known_for_department='Directing' ORDER BY name LIMIT 100")["name"].tolist()
    selected_director = st.selectbox("Director", ["All"] + directors)

    collections = get_df("SELECT DISTINCT collection_name FROM movies WHERE collection_name IS NOT NULL ORDER BY collection_name")["collection_name"].tolist()
    selected_collection = st.selectbox("Collection", ["All"] + collections)

    # genre handling (optional table)
    tables = get_df("SELECT name FROM sqlite_master WHERE type='table'")
    if "movie_genres" in tables["name"].tolist():
        genres = get_df("SELECT DISTINCT genre FROM movie_genres ORDER BY genre")["genre"].tolist()
        selected_genre = st.selectbox("Genre", ["All"] + genres)
    else:
        selected_genre = None

    rating_range = st.slider("Rating range (IMDb-style)", 0.0, 10.0, (0.0, 10.0))
    popularity_range = st.slider("Popularity range (current TMDB score)", 0.0, 400.0, (0.0, 400.0))

    # --- SQL query ---
    base_query = """
        SELECT DISTINCT m.id, m.title, m.year, m.vote_average, m.popularity, m.overview
        FROM movies m
        LEFT JOIN movie_people mp ON m.id = mp.movie_id
        LEFT JOIN people p ON mp.person_id = p.id
        WHERE 1=1
    """
    params = []
    if selected_year != "All":
        base_query += " AND m.year=?"
        params.append(selected_year)
    if selected_actor != "All":
        base_query += " AND p.name=? AND mp.role='Actor'"
        params.append(selected_actor)
    if selected_director != "All":
        base_query += " AND p.name=? AND mp.role='Director'"
        params.append(selected_director)
    if selected_collection != "All":
        base_query += " AND m.collection_name=?"
        params.append(selected_collection)
    if selected_genre and selected_genre != "All":
        base_query += " AND m.id IN (SELECT movie_id FROM movie_genres WHERE genre=?)"
        params.append(selected_genre)
    base_query += " AND m.vote_average BETWEEN ? AND ?"
    params.extend(rating_range)
    base_query += " AND m.popularity BETWEEN ? AND ?"
    params.extend(popularity_range)

    df = get_df(base_query, params)

    st.write(f"🎞️ Showing {len(df)} movies")

    # --- Field selector ---
    st.subheader("Select fields to display")
    all_fields = ["title", "year", "vote_average", "popularity", "overview"]
    if "selected_fields" not in st.session_state:
        st.session_state["selected_fields"] = ["title", "year", "vote_average"]
    selected_fields = st.session_state["selected_fields"]

    cols = st.columns(len(all_fields))
    for i, field in enumerate(all_fields):
        with cols[i]:
            checked = st.checkbox(field.capitalize().replace("_", " "), field in selected_fields)
            if checked and field not in selected_fields:
                selected_fields.append(field)
            elif not checked and field in selected_fields:
                selected_fields.remove(field)
    st.session_state["selected_fields"] = selected_fields

    df_display = df[selected_fields] if selected_fields else df[["title", "year", "vote_average"]]
    st.dataframe(df_display, use_container_width=True, hide_index=True)


# --------------------------------------------------
# TAB 2: Update
# --------------------------------------------------
with tab2:
    st.header("Add new movies to your database")
    st.caption("Use this tool to expand your local movie collection. It will **only add missing movies**, never overwrite.")

    year_to_add = st.number_input("Year to add", min_value=1900, max_value=2025, value=2024)
    page_count = st.slider("How many pages (20 movies each)?", 1, 10, 5)
    limit = page_count * 20

    if st.button("🔄 Fetch and Add Movies"):
        conn = sqlite3.connect(DB_PATH)
        existing_ids = {row[0] for row in conn.execute("SELECT id FROM movies")}
        movies = discover_movies(year_to_add, pages=page_count, limit=limit)
        added = 0
        progress = st.progress(0)

        for i, m in enumerate(movies):
            if m["id"] in existing_ids:
                continue
            detail = fetch_movie_detail(m["id"])
            if not detail:
                continue
            insert_movie(conn, detail)
            insert_people(conn, m["id"], detail.get("credits"))
            insert_genres(conn, m["id"], detail.get("genres"))
            conn.commit()
            added += 1
            progress.progress((i + 1) / len(movies))
        conn.close()

        st.success(f"✅ Added {added} new movies for {year_to_add} (skipped existing).")
