"""
DB Enrichment Tool — add movies to movies.sqlite via TMDB.

Search by: actor, director, keyword, collection, studio, or direct title/TMDB ID.
Results are previewed first; import on demand.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
import streamlit as st

from app_config import (
    DB_PATH,
    GENRES,
    get_db_connection,
    get_secret,
    is_turso_configured,
)

TMDB_API_KEY = get_secret("TMDB_API_KEY") or ""
TMDB_BASE_URL = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w92"

st.set_page_config(page_title="DB Enrichment", layout="wide", initial_sidebar_state="expanded")


# ── DB helpers ──────────────────────────────────────────────────────────────

def get_connection():
    return get_db_connection()


def movie_exists(movie_id: int) -> bool:
    conn = get_connection()
    row = conn.execute("SELECT 1 FROM movies WHERE movie_id=?", (movie_id,)).fetchone()
    conn.close()
    return row is not None


def upsert_movies(movies: List[Dict[str, Any]]) -> int:
    """Insert/update a list of TMDB movie dicts. Returns number of rows affected."""
    if not movies:
        return 0
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    count = 0
    for m in movies:
        movie_id = m.get("id")
        if not movie_id:
            continue
        release_date = m.get("release_date") or ""
        year = int(release_date[:4]) if len(release_date) >= 4 and release_date[:4].isdigit() else None

        # Genres: TMDB returns either genre objects or genre_ids
        genres_val = m.get("genres") or []
        if genres_val and isinstance(genres_val[0], dict):
            genres_str = ", ".join(g.get("name", "") for g in genres_val)
        else:
            # genre_ids — map to names using GENRES
            id_to_name = {v: k for k, v in GENRES.items() if v is not None}
            genres_str = ", ".join(id_to_name[gid] for gid in m.get("genre_ids", []) if gid in id_to_name)

        conn.execute("""
            INSERT INTO movies (movie_id, title, year, release_date, tmdb_rating, tmdb_votes,
                                genres, overview, poster_path, popularity, last_updated)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(movie_id) DO UPDATE SET
                title      = excluded.title,
                year       = COALESCE(excluded.year,       movies.year),
                release_date = COALESCE(NULLIF(excluded.release_date,''), movies.release_date),
                tmdb_rating  = COALESCE(excluded.tmdb_rating,  movies.tmdb_rating),
                tmdb_votes   = COALESCE(excluded.tmdb_votes,   movies.tmdb_votes),
                genres     = COALESCE(NULLIF(excluded.genres,''), movies.genres),
                overview   = COALESCE(NULLIF(excluded.overview,''), movies.overview),
                poster_path= COALESCE(NULLIF(excluded.poster_path,''), movies.poster_path),
                popularity = COALESCE(excluded.popularity, movies.popularity),
                last_updated = excluded.last_updated
        """, (
            movie_id,
            m.get("title") or m.get("original_title") or "Untitled",
            year,
            release_date,
            m.get("vote_average") or m.get("tmdb_rating"),
            m.get("vote_count") or m.get("tmdb_votes"),
            genres_str,
            m.get("overview", ""),
            m.get("poster_path", ""),
            m.get("popularity", 0.0),
            now,
        ))
        count += 1
    conn.commit()
    conn.close()
    return count


# ── TMDB helpers ─────────────────────────────────────────────────────────────

def tmdb_get(path: str, params: Optional[Dict] = None) -> dict:
    p = {"api_key": TMDB_API_KEY, "language": "en-US"}
    if params:
        p.update(params)
    try:
        r = requests.get(f"{TMDB_BASE_URL}{path}", params=p, timeout=10)
        return r.json() if r.status_code == 200 else {}
    except Exception:
        return {}


def search_person(name: str) -> List[Dict]:
    data = tmdb_get("/search/person", {"query": name, "page": 1})
    return data.get("results", [])[:8]


def search_keyword(term: str) -> List[Dict]:
    data = tmdb_get("/search/keyword", {"query": term, "page": 1})
    return data.get("results", [])[:8]


def search_collection(term: str) -> List[Dict]:
    data = tmdb_get("/search/collection", {"query": term, "page": 1})
    return data.get("results", [])[:8]


def search_company(term: str) -> List[Dict]:
    data = tmdb_get("/search/company", {"query": term, "page": 1})
    return data.get("results", [])[:8]


def search_movie_title(title: str, max_results: int = 20) -> List[Dict]:
    movies = []
    for page in range(1, 4):
        data = tmdb_get("/search/movie", {"query": title, "page": page})
        movies.extend(data.get("results", []))
        if len(movies) >= max_results or page >= data.get("total_pages", 1):
            break
    return movies[:max_results]


def fetch_movie_by_id(tmdb_id: int) -> Optional[Dict]:
    return tmdb_get(f"/movie/{tmdb_id}") or None


def discover_by_person(person_id: int, role: str, max_results: int = 100) -> List[Dict]:
    """Fetch movies via /discover using with_cast or with_crew."""
    key = "with_cast" if role == "actor" else "with_crew"
    movies = []
    page = 1
    while len(movies) < max_results:
        data = tmdb_get("/discover/movie", {
            key: person_id,
            "sort_by": "popularity.desc",
            "page": page,
        })
        results = data.get("results", [])
        if not results:
            break
        movies.extend(results)
        if page >= data.get("total_pages", 1) or page >= 5:
            break
        page += 1
    return movies[:max_results]


def discover_by_keyword(keyword_id: int, max_results: int = 100) -> List[Dict]:
    movies = []
    page = 1
    while len(movies) < max_results:
        data = tmdb_get("/discover/movie", {
            "with_keywords": keyword_id,
            "sort_by": "popularity.desc",
            "page": page,
        })
        results = data.get("results", [])
        if not results:
            break
        movies.extend(results)
        if page >= data.get("total_pages", 1) or page >= 5:
            break
        page += 1
    return movies[:max_results]


def fetch_collection_movies(collection_id: int) -> List[Dict]:
    data = tmdb_get(f"/collection/{collection_id}")
    return data.get("parts", [])


def discover_by_company(company_id: int, max_results: int = 100) -> List[Dict]:
    movies = []
    page = 1
    while len(movies) < max_results:
        data = tmdb_get("/discover/movie", {
            "with_companies": company_id,
            "sort_by": "popularity.desc",
            "page": page,
        })
        results = data.get("results", [])
        if not results:
            break
        movies.extend(results)
        if page >= data.get("total_pages", 1) or page >= 5:
            break
        page += 1
    return movies[:max_results]


# ── UI helpers ───────────────────────────────────────────────────────────────

def render_preview(movies: List[Dict], key_prefix: str) -> List[Dict]:
    """Show a preview table; returns the list unchanged."""
    if not movies:
        st.info("No results found.")
        return []

    new_count = sum(1 for m in movies if not movie_exists(m.get("id", 0)))
    st.caption(f"{len(movies)} movies found · **{new_count} new** (not yet in movies.sqlite)")

    rows = []
    for m in movies:
        mid = m.get("id", 0)
        rows.append({
            "✓ New": "🆕" if not movie_exists(mid) else "✔",
            "Title": m.get("title") or m.get("original_title", ""),
            "Year": (m.get("release_date") or "")[:4] or "—",
            "TMDB ⭐": m.get("vote_average") or "—",
            "Popularity": round(m.get("popularity") or 0, 1),
            "Overview": (m.get("overview") or "")[:120] + ("…" if len(m.get("overview") or "") > 120 else ""),
        })

    st.dataframe(rows, hide_index=True, width="stretch")
    return movies


def do_import(movies: List[Dict], label: str) -> None:
    if not movies:
        return
    with st.spinner(f"Importing {len(movies)} movies…"):
        n = upsert_movies(movies)
    st.success(f"✅ Imported / updated **{n}** movies into movies.sqlite from {label}.")


# ── Main app ──────────────────────────────────────────────────────────────────

st.title(":material/database: DB Enrichment Tool")
st.caption("Add movies to `movies.sqlite` by searching TMDB. Preview results before importing.")

if not TMDB_API_KEY:
    st.error("TMDB_API_KEY not found. Add it to `.streamlit/secrets.toml`.")
    st.stop()

conn = get_connection()
total_movies = conn.execute("SELECT COUNT(*) FROM movies").fetchone()[0]
conn.close()
st.sidebar.metric("Movies in DB", f"{total_movies:,}")
if is_turso_configured():
    st.sidebar.caption("🟢 Connected to: **Turso Cloud**")
else:
    st.sidebar.caption("📁 Connected to: **Local movies.sqlite**")

tab_person, tab_keyword, tab_collection, tab_company, tab_title = st.tabs([
    "🎭 Actor / Director",
    "🏷️ Keyword",
    "🎬 Collection",
    "🏢 Studio",
    "🔍 Title / TMDB ID",
])

# ── Tab 1: Actor / Director ──────────────────────────────────────────────────
with tab_person:
    st.subheader("Search by actor or director")
    col_name, col_role = st.columns([3, 1])
    with col_name:
        person_query = st.text_input("Person name", placeholder="e.g. Cate Blanchett", key="person_query")
    with col_role:
        person_role = st.selectbox("Role", ["Actor", "Director"], key="person_role")

    if person_query:
        results = search_person(person_query)
        if not results:
            st.warning("No person found.")
        else:
            options = {f"{p['name']} ({p.get('known_for_department','')})": p["id"] for p in results}
            chosen_label = st.selectbox("Select person", list(options.keys()), key="person_select")
            chosen_id = options[chosen_label]
            max_r = st.slider("Max movies to fetch", 20, 200, 100, 20, key="person_max")

            if st.button("Preview movies", key="person_preview"):
                with st.spinner("Fetching from TMDB…"):
                    movies = discover_by_person(chosen_id, person_role.lower(), max_r)
                st.session_state["person_movies"] = movies

        if "person_movies" in st.session_state:
            movies = render_preview(st.session_state["person_movies"], "person_movies")
            if st.button("Import all into movies.sqlite", type="primary", key="person_import"):
                do_import(st.session_state["person_movies"], chosen_label)
                del st.session_state["person_movies"]

# ── Tab 2: Keyword ───────────────────────────────────────────────────────────
with tab_keyword:
    st.subheader("Search by keyword")
    kw_query = st.text_input("Keyword", placeholder="e.g. time travel, heist, samurai", key="kw_query")

    if kw_query:
        kw_results = search_keyword(kw_query)
        if not kw_results:
            st.warning("No keyword found.")
        else:
            kw_options = {f"{k['name']} (id {k['id']})": k["id"] for k in kw_results}
            chosen_kw_label = st.selectbox("Select keyword", list(kw_options.keys()), key="kw_select")
            chosen_kw_id = kw_options[chosen_kw_label]
            max_kw = st.slider("Max movies to fetch", 20, 200, 100, 20, key="kw_max")

            if st.button("Preview movies", key="kw_preview"):
                with st.spinner("Fetching from TMDB…"):
                    movies = discover_by_keyword(chosen_kw_id, max_kw)
                st.session_state["kw_movies"] = movies

        if "kw_movies" in st.session_state:
            render_preview(st.session_state["kw_movies"], "kw_movies")
            if st.button("Import all into movies.sqlite", type="primary", key="kw_import"):
                do_import(st.session_state["kw_movies"], chosen_kw_label)
                del st.session_state["kw_movies"]

# ── Tab 3: Collection ────────────────────────────────────────────────────────
with tab_collection:
    st.subheader("Search by collection / franchise")
    coll_query = st.text_input("Collection name", placeholder="e.g. James Bond, Marvel, Lord of the Rings", key="coll_query")

    if coll_query:
        coll_results = search_collection(coll_query)
        if not coll_results:
            st.warning("No collection found.")
        else:
            coll_options = {c["name"]: c["id"] for c in coll_results}
            chosen_coll_label = st.selectbox("Select collection", list(coll_options.keys()), key="coll_select")
            chosen_coll_id = coll_options[chosen_coll_label]

            if st.button("Preview movies", key="coll_preview"):
                with st.spinner("Fetching collection parts from TMDB…"):
                    movies = fetch_collection_movies(chosen_coll_id)
                st.session_state["coll_movies"] = movies

        if "coll_movies" in st.session_state:
            render_preview(st.session_state["coll_movies"], "coll_movies")
            if st.button("Import all into movies.sqlite", type="primary", key="coll_import"):
                do_import(st.session_state["coll_movies"], chosen_coll_label)
                del st.session_state["coll_movies"]

# ── Tab 4: Studio ────────────────────────────────────────────────────────────
with tab_company:
    st.subheader("Search by production studio / company")
    co_query = st.text_input("Company name", placeholder="e.g. A24, Pixar, Studio Ghibli", key="co_query")

    if co_query:
        co_results = search_company(co_query)
        if not co_results:
            st.warning("No company found.")
        else:
            co_options = {c["name"]: c["id"] for c in co_results}
            chosen_co_label = st.selectbox("Select company", list(co_options.keys()), key="co_select")
            chosen_co_id = co_options[chosen_co_label]
            max_co = st.slider("Max movies to fetch", 20, 200, 100, 20, key="co_max")

            if st.button("Preview movies", key="co_preview"):
                with st.spinner("Fetching from TMDB…"):
                    movies = discover_by_company(chosen_co_id, max_co)
                st.session_state["co_movies"] = movies

        if "co_movies" in st.session_state:
            render_preview(st.session_state["co_movies"], "co_movies")
            if st.button("Import all into movies.sqlite", type="primary", key="co_import"):
                do_import(st.session_state["co_movies"], chosen_co_label)
                del st.session_state["co_movies"]

# ── Tab 5: Title / TMDB ID ───────────────────────────────────────────────────
with tab_title:
    st.subheader("Search by title or import by TMDB ID")

    col_t, col_id = st.columns([3, 1])
    with col_t:
        title_query = st.text_input("Movie title", placeholder="e.g. Parasite", key="title_query")
    with col_id:
        tmdb_id_input = st.number_input("…or TMDB ID", min_value=0, value=0, step=1, key="tmdb_id_input")

    if tmdb_id_input > 0:
        if st.button("Fetch by TMDB ID", key="id_fetch"):
            with st.spinner("Fetching…"):
                movie = fetch_movie_by_id(int(tmdb_id_input))
            if movie and movie.get("id"):
                st.session_state["id_movies"] = [movie]
            else:
                st.error("Movie not found.")

        if "id_movies" in st.session_state:
            render_preview(st.session_state["id_movies"], "id_movies")
            if st.button("Import into movies.sqlite", type="primary", key="id_import"):
                do_import(st.session_state["id_movies"], f"TMDB ID {tmdb_id_input}")
                del st.session_state["id_movies"]

    elif title_query:
        max_title = st.slider("Max results", 10, 80, 20, 10, key="title_max")
        if st.button("Search", key="title_search"):
            with st.spinner(f"Searching TMDB for '{title_query}'…"):
                movies = search_movie_title(title_query, max_title)
            st.session_state["title_movies"] = movies

        if "title_movies" in st.session_state:
            render_preview(st.session_state["title_movies"], "title_movies")
            if st.button("Import all into movies.sqlite", type="primary", key="title_import"):
                do_import(st.session_state["title_movies"], f"title search '{title_query}'")
                del st.session_state["title_movies"]
