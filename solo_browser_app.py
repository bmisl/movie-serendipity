
"""Solo movie browser - GLOBAL local SQLite database + incremental sync on launch.
Design: movies table = 1 row per movie globally (no duplication)
        availability table = tiny per-region services
        sync_log = last sync per region
"""

from __future__ import annotations

import math
import os
import sqlite3
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from itertools import zip_longest
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import pandas as pd
import requests
import streamlit as st
from st_aggrid import (
    AgGrid,
    ColumnsAutoSizeMode,
    DataReturnMode,
    GridOptionsBuilder,
    GridUpdateMode,
)

from app_config import GENRES, REGION_PROVIDERS, REGIONS, DB_PATH, get_secret

TMDB_BASE_URL = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w342"
REQUEST_TIMEOUT = 10
PAGE_SIZE = 20
DEFAULT_MAX_RESULTS = 400
MAX_RESULTS_LIMIT = 1000
DETAIL_MAX_WORKERS = 4
DETAIL_QUEUE_LIMIT = 32
DETAIL_POLL_SECONDS = 5
TMDB_SEARCH_MAX_RESULTS = 80
SELECTION_MODES = {
    "Balanced 50/50": "balanced",
    "Recent": "primary_release_date.desc",
    "Popular": "popularity.desc",
}

DB_SYNC_TTL_HOURS = 12
NEW_MOVIE_LOOKBACK_DAYS = 14

GENRE_ID_TO_NAME = {
    genre_id: genre_name for genre_name, genre_id in GENRES.items() if genre_id is not None
}
PROVIDER_BUCKET_LABELS = {"flatrate": "", "free": "", "ads": ""}  # v16 FI/DK/IS fast  # v16 streaming-only FI/DK/IS

st.set_page_config(
    page_title="Solo Movie Browser",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ==================== GLOBAL DB LAYER ====================

def get_db_path() -> Path:
    return Path(DB_PATH)

def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(str(get_db_path()), check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn

def init_global_db():
    db_path = get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = get_connection()
    cur = conn.cursor()
    # Global movie metadata - ONE row per movie_id
    cur.execute("""
    CREATE TABLE IF NOT EXISTS movies (
        movie_id INTEGER PRIMARY KEY,
        title TEXT NOT NULL,
        year INTEGER,
        release_date TEXT,
        rating REAL,
        votes INTEGER,
        genres TEXT,
        overview TEXT,
        poster_path TEXT,
        popularity REAL,
        runtime INTEGER,
        directors TEXT,
        actors TEXT,
        last_updated TEXT
    )
    """)
    # Per-region availability - tiny rows
    cur.execute("""
    CREATE TABLE IF NOT EXISTS availability (
        movie_id INTEGER NOT NULL,
        region_code TEXT NOT NULL,
        services TEXT,
        last_updated TEXT,
        PRIMARY KEY (movie_id, region_code),
        FOREIGN KEY (movie_id) REFERENCES movies(movie_id) ON DELETE CASCADE
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS sync_log (
        region_code TEXT PRIMARY KEY,
        last_sync TEXT NOT NULL,
        movies_synced INTEGER DEFAULT 0
    )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_movies_year ON movies(year)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_movies_pop ON movies(popularity DESC)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_avail_region ON availability(region_code)")
    conn.commit()
    conn.close()

def get_last_sync(region_code: str) -> Optional[datetime]:
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT last_sync FROM sync_log WHERE region_code=?", (region_code,))
        row = cur.fetchone()
        conn.close()
        if row:
            return datetime.fromisoformat(row["last_sync"])
    except Exception:
        pass
    return None

def set_last_sync(region_code: str, count: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO sync_log(region_code, last_sync, movies_synced)
        VALUES(?,?,?)
        ON CONFLICT(region_code) DO UPDATE SET last_sync=excluded.last_sync, movies_synced=excluded.movies_synced
    """, (region_code, datetime.now(timezone.utc).isoformat(), count))
    conn.commit()
    conn.close()

def upsert_movies_global(movies: List[dict]):
    if not movies:
        return
    conn = get_connection()
    cur = conn.cursor()
    now = datetime.now(timezone.utc).isoformat()
    for m in movies:
        cur.execute("""
            INSERT INTO movies(movie_id, title, year, release_date, rating, votes, genres, overview, poster_path, popularity, runtime, directors, actors, last_updated)
            VALUES(:movie_id, :title, :year, :release_date, :rating, :votes, :genres, :overview, :poster_path, :popularity, :runtime, :directors, :actors, :last_updated)
            ON CONFLICT(movie_id) DO UPDATE SET
                title=COALESCE(NULLIF(excluded.title,''), title),
                year=COALESCE(excluded.year, year),
                release_date=COALESCE(NULLIF(excluded.release_date,''), release_date),
                rating=COALESCE(excluded.rating, rating),
                votes=COALESCE(excluded.votes, votes),
                genres=COALESCE(NULLIF(excluded.genres,''), genres),
                overview=COALESCE(NULLIF(excluded.overview,''), overview),
                poster_path=COALESCE(NULLIF(excluded.poster_path,''), poster_path),
                popularity=MAX(COALESCE(excluded.popularity,0), COALESCE(popularity,0)),
                runtime=COALESCE(excluded.runtime, runtime),
                directors=COALESCE(NULLIF(excluded.directors,''), directors),
                actors=COALESCE(NULLIF(excluded.actors,''), actors),
                last_updated=excluded.last_updated
        """, {
            "movie_id": m.get("movie_id"),
            "title": m.get("title",""),
            "year": m.get("year"),
            "release_date": m.get("release_date",""),
            "rating": m.get("rating"),
            "votes": m.get("votes"),
            "genres": m.get("genres",""),
            "overview": m.get("overview",""),
            "poster_path": m.get("poster_path",""),
            "popularity": m.get("popularity",0),
            "runtime": m.get("runtime"),
            "directors": m.get("directors",""),
            "actors": m.get("actors",""),
            "last_updated": now,
        })
    conn.commit()
    conn.close()

def upsert_availability(movie_id: int, region_code: str, services: str):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO availability(movie_id, region_code, services, last_updated)
        VALUES(?,?,?,?)
        ON CONFLICT(movie_id, region_code) DO UPDATE SET services=excluded.services, last_updated=excluded.last_updated
    """, (movie_id, region_code, services, datetime.now(timezone.utc).isoformat()))
    conn.commit()
    conn.close()

def upsert_availability_batch(items: List[Tuple[int,str,str]]):
    if not items:
        return
    conn = get_connection()
    cur = conn.cursor()
    now = datetime.now(timezone.utc).isoformat()
    cur.executemany("""
        INSERT INTO availability(movie_id, region_code, services, last_updated)
        VALUES(?,?,?,?)
        ON CONFLICT(movie_id, region_code) DO UPDATE SET services=COALESCE(NULLIF(excluded.services,''), services), last_updated=excluded.last_updated
    """, [(mid, rc, svc, now) for mid, rc, svc in items])
    conn.commit()
    conn.close()

def update_movie_details_global(movie_id: int, directors: str, actors: str, runtime: Optional[int], services: Optional[str], region_code: str):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        UPDATE movies SET directors=COALESCE(NULLIF(?,''), directors),
                          actors=COALESCE(NULLIF(?,''), actors),
                          runtime=COALESCE(?, runtime),
                          last_updated=?
        WHERE movie_id=?
    """, (directors, actors, runtime, datetime.now(timezone.utc).isoformat(), movie_id))
    conn.commit()
    conn.close()
    if services:
        upsert_availability(movie_id, region_code, services)

def fetch_from_global_db(
    region_code: str,
    genre_id: Optional[int],
    max_results: int,
    search_text: str = "",
    streaming_only: bool = True,
    selected_services: Optional[Sequence[str]] = None,
) -> List[dict]:
    """Fast indexed SQL fetch from global DB with tokenized search & region/service filtering."""
    conn = get_connection()
    try:
        params = []
        if streaming_only:
            query = """
            SELECT m.movie_id, m.title, m.year, m.release_date, m.rating, m.votes,
                   m.genres, m.overview, m.poster_path, m.popularity, m.runtime,
                   m.directors, m.actors, COALESCE(a.services, '') as services
            FROM movies m
            JOIN availability a ON m.movie_id = a.movie_id AND a.region_code = ?
            WHERE (a.services IS NOT NULL AND a.services != '')
            """
            params.append(region_code)
        else:
            query = """
            SELECT m.movie_id, m.title, m.year, m.release_date, m.rating, m.votes,
                   m.genres, m.overview, m.poster_path, m.popularity, m.runtime,
                   m.directors, m.actors, COALESCE(a.services, '') as services
            FROM movies m
            LEFT JOIN availability a ON m.movie_id = a.movie_id AND a.region_code = ?
            WHERE 1=1
            """
            params.append(region_code)

        if genre_id is not None:
            genre_name = GENRE_ID_TO_NAME.get(genre_id)
            if genre_name:
                query += " AND m.genres LIKE ?"
                params.append(f"%{genre_name}%")

        if search_text and search_text.strip():
            tokens = search_text.strip().split()
            for token in tokens:
                query += " AND (m.title LIKE ? OR m.genres LIKE ? OR m.directors LIKE ? OR m.actors LIKE ? OR m.overview LIKE ?)"
                pattern = f"%{token}%"
                params.extend([pattern] * 5)

        query += " ORDER BY m.popularity DESC"

        if not (streaming_only and selected_services):
            query += " LIMIT ?"
            params.append(max_results)

        df = pd.read_sql_query(query, conn, params=params)
        conn.close()

        if df.empty:
            return []

        if streaming_only and selected_services:
            selected_set = set(selected_services)
            def matches_services(svc_str: str) -> bool:
                if not svc_str:
                    return False
                item_svcs = [s.strip() for s in svc_str.split(",")]
                return any(s in selected_set for s in item_svcs)

            df = df[df["services"].apply(matches_services)]
            df = df.head(max_results)

        recs = df.to_dict(orient="records")
        for r in recs:
            r.setdefault("directors", "")
            r.setdefault("actors", "")
            r.setdefault("services", "")
            r.setdefault("poster_path", "")
            r.setdefault("overview", "")
        return recs
    except Exception as e:
        try: conn.close()
        except: pass
        print(f"DB fetch error {e}")
        return []



def count_db_global() -> Tuple[int,int]:
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM movies")
        mc = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM availability")
        ac = cur.fetchone()[0]
        conn.close()
        return mc, ac
    except Exception:
        return 0,0

# ==================== TMDB HELPERS (same as original) ====================

def resolve_tmdb_key() -> str:
    key = get_secret("TMDB_API_KEY")
    if not key:
        key = os.getenv("TMDB_API_KEY", "")
    return key

TMDB_API_KEY = resolve_tmdb_key()

def tmdb_api_get(path: str, params: Optional[Dict[str, object]] = None) -> dict:
    if not TMDB_API_KEY:
        st.error("Missing TMDB_API_KEY. Configure `.streamlit/secrets.toml` or set the environment variable.")
        st.stop()
    payload: Dict[str, object] = {"api_key": TMDB_API_KEY, "language": "en-US"}
    if params:
        payload.update(params)
    try:
        response = requests.get(f"{TMDB_BASE_URL}{path}", params=payload, timeout=REQUEST_TIMEOUT)
        if response.status_code == 200:
            return response.json()
        return {}
    except requests.RequestException:
        return {}

@st.cache_resource
def get_detail_executor() -> ThreadPoolExecutor:
    return ThreadPoolExecutor(max_workers=DETAIL_MAX_WORKERS, thread_name_prefix="tmdb-detail")

def build_discover_params(region_code: str, provider_ids: Sequence[int], genre_id: Optional[int], page: int, streaming_only: bool, sort_by: str, extra: Optional[Dict]=None) -> Dict[str, object]:
    params: Dict[str, object] = {
        "sort_by": sort_by,
        "include_adult": "false",
        "include_video": "false",
        "page": page,
        "region": region_code,
        "watch_region": region_code,
    }
    if streaming_only:
        params["with_ott_monetization_types"] = "flatrate|free|ads"
    if provider_ids:
        params["with_watch_providers"] = "|".join(str(pid) for pid in provider_ids)
    if genre_id is not None:
        params["with_genres"] = genre_id
    if extra:
        params.update(extra)
    return params

@st.cache_data(ttl=3600, show_spinner=False)
def fetch_discover_movies_for_sort(region_code: str, provider_ids: Tuple[int, ...], genre_id: Optional[int], max_results: int, streaming_only: bool, sort_by: str, release_gte: Optional[str]=None) -> List[dict]:
    movies: List[dict] = []
    seen_ids: set[int] = set()
    page = 1
    max_pages = max(1, math.ceil(max_results / PAGE_SIZE))
    extra = {}
    if release_gte:
        extra["primary_release_date.gte"] = release_gte
    while page <= max_pages and len(movies) < max_results:
        payload = tmdb_api_get("/discover/movie", build_discover_params(region_code, provider_ids, genre_id, page, streaming_only, sort_by, extra))
        results = payload.get("results", []) or []
        if not results:
            break
        for movie in results:
            movie_id = movie.get("id")
            if not movie_id or movie_id in seen_ids:
                continue
            seen_ids.add(movie_id)
            release_date = movie.get("release_date") or ""
            year = int(release_date[:4]) if len(release_date) >= 4 and release_date[:4].isdigit() else None
            genre_names = [GENRE_ID_TO_NAME.get(gid) for gid in movie.get("genre_ids", []) if GENRE_ID_TO_NAME.get(gid)]
            movies.append({
                "movie_id": movie_id,
                "title": movie.get("title") or movie.get("original_title") or "",
                "year": year,
                "rating": round(float(movie.get("vote_average") or 0.0), 1),
                "votes": int(movie.get("vote_count") or 0),
                "genres": ", ".join(genre_names),
                "overview": movie.get("overview") or "",
                "poster_path": movie.get("poster_path") or "",
                "popularity": float(movie.get("popularity") or 0.0),
                "directors": "",
                "actors": "",
                "services": "",
                "release_date": release_date,
                "runtime": None,
            })
            if len(movies) >= max_results:
                break
        total_pages = int(payload.get("total_pages") or page)
        if page >= total_pages:
            break
        page += 1
    return movies

@st.cache_data(ttl=3600, show_spinner=False)
def fetch_tmdb_search_movies(region_code: str, provider_ids: Tuple[int, ...], genre_id: Optional[int], query: str, max_results: int, streaming_only: bool) -> List[dict]:
    movies: List[dict] = []
    seen_ids: set[int] = set()
    page = 1
    max_pages = max(1, math.ceil(max_results / PAGE_SIZE))
    while page <= max_pages and len(movies) < max_results:
        payload = tmdb_api_get("/search/movie", {"query": query, "page": page, "include_adult": "false", "region": region_code})
        results = payload.get("results", []) or []
        if not results:
            break
        for movie in results:
            movie_id = movie.get("id")
            if not movie_id or movie_id in seen_ids:
                continue
            seen_ids.add(movie_id)
            release_date = movie.get("release_date") or ""
            year = int(release_date[:4]) if len(release_date) >= 4 and release_date[:4].isdigit() else None
            genre_names = [GENRE_ID_TO_NAME.get(gid) for gid in movie.get("genre_ids", []) if GENRE_ID_TO_NAME.get(gid)]
            movies.append({
                "movie_id": movie_id,
                "title": movie.get("title") or movie.get("original_title") or "",
                "year": year,
                "rating": round(float(movie.get("vote_average") or 0.0), 1),
                "votes": int(movie.get("vote_count") or 0),
                "genres": ", ".join(genre_names),
                "overview": movie.get("overview") or "",
                "poster_path": movie.get("poster_path") or "",
                "popularity": float(movie.get("popularity") or 0.0),
                "directors": "",
                "actors": "",
                "services": "",
                "release_date": release_date,
                "runtime": None,
            })
            if len(movies) >= max_results:
                break
        total_pages = int(payload.get("total_pages") or page)
        if page >= total_pages:
            break
        page += 1
    return movies

@st.cache_data(ttl=3600, show_spinner=False)
def fetch_movie_watch_providers(movie_id: int, region_code: str) -> dict:
    payload = tmdb_api_get(f"/movie/{movie_id}/watch/providers")
    return payload.get("results", {}).get(region_code, {}) if payload else {}

def movie_matches_selected_services(movie_id: int, region_code: str, selected_service_names: Sequence[str]) -> bool:
    if not selected_service_names:
        return True
    region_services = REGION_PROVIDERS.get(region_code, {})
    wanted_ids = {region_services[name] for name in selected_service_names if name in region_services}
    if not wanted_ids:
        return False
    provider_data = fetch_movie_watch_providers(movie_id, region_code)
    for bucket in ("flatrate", "free", "ads"):
        for entry in provider_data.get(bucket, []) or []:
            if entry.get("provider_id") in wanted_ids:
                return True
    return False

@st.cache_data(ttl=3600, show_spinner=False)
def fetch_movie_enrichment(movie_id: int, region_code: str) -> dict:
    payload = tmdb_api_get(f"/movie/{movie_id}", {"append_to_response": "credits,watch/providers"})
    if not payload:
        return {}
    credits = payload.get("credits") or {}
    cast = [m.get("name") for m in credits.get("cast", [])[:10] if m and m.get("name")]
    directors = [m.get("name") for m in credits.get("crew", []) if m and m.get("job")=="Director" and m.get("name")]
    region_providers = payload.get("watch/providers", {}).get("results", {}).get(region_code, {})
    all_names: List[str] = []
    for bucket in ("flatrate","free","ads"):
        names = [p.get("provider_name") for p in region_providers.get(bucket, []) or [] if p and p.get("provider_name")]
        all_names.extend(names)
    uniq = sorted(dict.fromkeys(all_names))
    return {
        "movie_id": movie_id,
        "cast": cast,
        "directors": directors,
        "services": ", ".join(uniq) if uniq else "",
        "runtime": payload.get("runtime"),
    }

def merge_movie_lists(primary_movies: List[dict], secondary_movies: List[dict], max_results: int) -> List[dict]:
    merged: List[dict] = []
    seen_ids: set[int] = set()
    for pm, sm in zip_longest(primary_movies, secondary_movies):
        for movie in (pm, sm):
            if not movie:
                continue
            mid = movie.get("movie_id")
            if mid in seen_ids:
                continue
            seen_ids.add(mid)
            merged.append(movie)
            if len(merged) >= max_results:
                return merged
    return merged

@st.cache_data(ttl=3600, show_spinner=False)
def fetch_discover_movies(region_code: str, provider_ids: Tuple[int, ...], genre_id: Optional[int], max_results: int, streaming_only: bool, selection_mode: str) -> List[dict]:
    if selection_mode == "balanced":
        recent_target = (max_results + 1)//2
        popular_target = max_results - recent_target
        recent_movies = fetch_discover_movies_for_sort(region_code, provider_ids, genre_id, recent_target, streaming_only, "primary_release_date.desc")
        popular_movies = fetch_discover_movies_for_sort(region_code, provider_ids, genre_id, popular_target, streaming_only, "popularity.desc")
        return merge_movie_lists(recent_movies, popular_movies, max_results)
    return fetch_discover_movies_for_sort(region_code, provider_ids, genre_id, max_results, streaming_only, selection_mode)

def humanize_runtime(runtime: Optional[int]) -> str:
    if not runtime:
        return ""
    h,m = divmod(runtime,60)
    return f"{h}h {m}m" if h else f"{m}m"

def ensure_session_state():
    st.session_state.setdefault("detail_cache", {})
    st.session_state.setdefault("detail_futures", {})
    st.session_state.setdefault("detail_queue", [])
    st.session_state.setdefault("detail_signature", None)
    st.session_state.setdefault("detail_complete", False)
    st.session_state.setdefault("selected_movie_id", None)
    st.session_state.setdefault("selected_movie_title", None)
    st.session_state.setdefault("dismissed_movie_id", None)
    st.session_state.setdefault("launch_sync_done", False)

def detail_key(movie_id: int, region_code: str) -> Tuple[int,str]:
    return movie_id, region_code

def drain_completed_detail_jobs():
    cache: Dict[Tuple[int,str], dict] = st.session_state.detail_cache
    futures: Dict[Tuple[int,str], Future] = st.session_state.detail_futures
    for key, future in list(futures.items()):
        if not future.done():
            continue
        try:
            result = future.result()
        except Exception:
            result = {}
        if result:
            cache[key] = result
            # Write back to global DB
            try:
                dirs = ", ".join(result.get("directors",[]))
                cast = ", ".join(result.get("cast",[]))
                update_movie_details_global(key[0], dirs, cast, result.get("runtime"), result.get("services"), key[1])
            except Exception:
                pass
        del futures[key]

def schedule_detail_jobs(movie_rows: Sequence[dict], region_code: str):
    ensure_session_state()
    movie_ids = [int(r["movie_id"]) for r in movie_rows if "movie_id" in r]
    queue_signature = (region_code, tuple(movie_ids))
    if st.session_state.detail_signature != queue_signature:
        st.session_state.detail_signature = queue_signature
        cache: Dict[Tuple[int,str], dict] = st.session_state.detail_cache
        needed = []
        for r in movie_rows:
            mid = int(r["movie_id"])
            key = detail_key(mid, region_code)
            if key in cache:
                continue
            has_dirs = bool(r.get("directors"))
            has_actors = bool(r.get("actors"))
            has_runtime = r.get("runtime") is not None
            has_services = bool(r.get("services"))
            if not (has_dirs and has_actors and has_runtime and has_services):
                needed.append(mid)
        st.session_state.detail_queue = needed
        st.session_state.detail_complete = (len(needed) == 0)

    cache: Dict[Tuple[int,str], dict] = st.session_state.detail_cache
    futures: Dict[Tuple[int,str], Future] = st.session_state.detail_futures
    queue: List[int] = st.session_state.detail_queue
    while queue and len(futures) < DETAIL_QUEUE_LIMIT:
        movie_id = queue.pop(0)
        key = detail_key(movie_id, region_code)
        if key in cache or key in futures:
            continue
        futures[key] = get_detail_executor().submit(fetch_movie_enrichment, movie_id, region_code)

def enrich_rows(movie_rows: List[dict], region_code: str) -> List[dict]:
    cache: Dict[Tuple[int,str], dict] = st.session_state.detail_cache
    enriched = []
    for row in movie_rows:
        key = detail_key(int(row["movie_id"]), region_code)
        merged = dict(row)
        detail = cache.get(key)
        if detail:
            if detail.get("directors"):
                merged["directors"] = ", ".join(detail.get("directors",[]))
            if detail.get("cast"):
                merged["actors"] = ", ".join(detail.get("cast",[]))
            if detail.get("services"):
                merged["services"] = detail.get("services","")
        enriched.append(merged)
    return enriched

def format_selected_row(selected_rows: object) -> Optional[dict]:
    if selected_rows is None:
        return None
    if isinstance(selected_rows, pd.DataFrame):
        return None if selected_rows.empty else selected_rows.iloc[0].to_dict()
    if isinstance(selected_rows, list):
        return selected_rows[0] if selected_rows else None
    if hasattr(selected_rows, "iloc"):
        try:
            return None if len(selected_rows)==0 else selected_rows.iloc[0].to_dict()
        except Exception:
            return None
    return None

@st.fragment(run_every=DETAIL_POLL_SECONDS)
def detail_status_fragment(movie_rows: Sequence[dict], region_code: str):
    drain_completed_detail_jobs()
    schedule_detail_jobs(movie_rows, region_code)
    if not st.session_state.detail_futures and not st.session_state.detail_queue:
        if not st.session_state.detail_complete:
            st.session_state.detail_complete = True
            st.rerun()
    else:
        st.caption("Background detail enrichment running - grid will refresh when done. Details are saved to global DB.")

def check_region_availability_changes(region_code: str, days: int = 7) -> Dict[str, int]:
    """Re-check movies in global DB for region_code to detect arrivals, dropouts, and switches."""
    conn = get_connection()
    cur = conn.cursor()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    cur.execute("SELECT movie_id, services FROM availability WHERE region_code=? AND (last_updated IS NULL OR last_updated < ?)", (region_code, cutoff))
    rows = cur.fetchall()
    if not rows:
        cur.execute("SELECT movie_id, services FROM availability WHERE region_code=?", (region_code,))
        rows = cur.fetchall()[:200]
    
    new_arrivals = 0
    left_count = 0
    switched_count = 0
    updates = []
    
    def parse_svcs(svc_str: str) -> set:
        if not svc_str: return set()
        return {s.strip() for s in svc_str.split(",") if s.strip()}
    
    def check_one(r):
        mid, old_svc = r["movie_id"], r["services"] or ""
        payload = tmdb_api_get(f"/movie/{mid}/watch/providers")
        prov = payload.get("results", {}).get(region_code, {}) if payload else {}
        all_names = []
        for b in ("flatrate", "free", "ads"):
            for p in prov.get(b, []) or []:
                n = p.get("provider_name")
                if n and n not in all_names:
                    all_names.append(n)
        uniq = sorted(dict.fromkeys(all_names))
        new_svc = ", ".join(uniq)
        return mid, old_svc, new_svc

    with ThreadPoolExecutor(max_workers=DETAIL_MAX_WORKERS, thread_name_prefix="check-changes") as ex:
        futs = [ex.submit(check_one, r) for r in rows[:100]]
        for fut in as_completed(futs):
            try:
                mid, old_svc, new_svc = fut.result()
                old_set = parse_svcs(old_svc)
                new_set = parse_svcs(new_svc)
                if not old_set and new_set:
                    new_arrivals += 1
                elif old_set and not new_set:
                    left_count += 1
                elif old_set != new_set:
                    switched_count += 1
                if old_svc != new_svc:
                    updates.append((mid, new_svc))
            except Exception:
                pass
    if updates:
        upsert_availability_batch([(mid, region_code, new_svc) for mid, new_svc in updates])
    return {
        "checked": len(rows[:100]),
        "arrivals": new_arrivals,
        "left": left_count,
        "switched": switched_count,
        "updated": len(updates),
    }

# ==================== LAUNCH SYNC ====================

def run_launch_sync_global(region_code: str, provider_ids: Tuple[int,...], streaming_only: bool, genre_id: Optional[int], max_results: int, selection_mode: str):
    last_sync = get_last_sync(region_code)
    needs_full = False
    if last_sync is None:
        needs_full = True
    elif datetime.now(timezone.utc) - last_sync > timedelta(hours=DB_SYNC_TTL_HOURS):
        needs_full = True

    new_movies: List[dict] = []
    if needs_full:
        # Full: balanced + recent
        new_movies = fetch_discover_movies(region_code, provider_ids, genre_id, max_results, streaming_only, selection_mode)
    else:
        since_date = (datetime.now(timezone.utc) - timedelta(days=NEW_MOVIE_LOOKBACK_DAYS)).date().isoformat()
        if last_sync:
            since_date = max(since_date, (last_sync - timedelta(days=3)).date().isoformat())
        # incremental: only new releases
        batch = fetch_discover_movies_for_sort(region_code, provider_ids, genre_id, 200, streaming_only, "primary_release_date.desc", release_gte=since_date)
        new_movies = batch

    # Dedupe and upsert globally
    seen = {}
    for m in new_movies:
        seen[m["movie_id"]] = m
    deduped = list(seen.values())
    if deduped:
        upsert_movies_global(deduped)
        # create empty availability rows so they appear for this region
        avail_batch = [(m["movie_id"], region_code, m.get("services","")) for m in deduped]
        upsert_availability_batch(avail_batch)
        set_last_sync(region_code, len(deduped))
        return len(deduped), False
    else:
        if needs_full:
            set_last_sync(region_code, 0)
        return 0, True

# ==================== MAIN ====================

def main():
    ensure_session_state()
    init_global_db()

    with st.sidebar:
        st.title("Solo Movie Browser")
        st.caption("Global DB: movies stored once, availability per region.")
        st.divider()
        region_name = st.selectbox("Region", list(REGIONS.keys()), index=0)
        region_code = REGIONS[region_name]
        genre_name = st.selectbox("Genre", list(GENRES.keys()), index=0)
        genre_id = GENRES[genre_name]
        if hasattr(st, "segmented_control"):
            sort_label = st.segmented_control("Selection mode", options=list(SELECTION_MODES.keys()), default=list(SELECTION_MODES.keys())[0], help="Balanced mixes recent and popular")
            if not sort_label:
                sort_label = list(SELECTION_MODES.keys())[0]
        else:
            sort_label = st.radio("Selection mode", list(SELECTION_MODES.keys()), index=0, horizontal=True, help="Balanced mixes recent and popular")
        selection_mode = SELECTION_MODES[sort_label]
        streaming_only = st.checkbox("Show only streaming titles", value=True)
        service_names = list(REGION_PROVIDERS.get(region_code, {}).keys())
        selected_service_names = st.multiselect("Streaming services", options=service_names, default=service_names, help="Used when streaming-only mode is enabled.")
        max_results = st.slider("Max titles to load", min_value=100, max_value=MAX_RESULTS_LIMIT, value=DEFAULT_MAX_RESULTS, step=100)
        title_search = st.text_input("Search titles, genres, plots, directors, or actors", value="", placeholder="e.g. comedy, nolan, space, denzel")
        if st.button("Refresh TMDB results"):
            st.cache_data.clear()
            st.session_state.detail_signature = None
            st.session_state.detail_queue = []
            st.session_state.detail_complete = False
            st.rerun()
        if st.button(f"Check arrivals & dropouts ({region_code})", help=f"Check for new arrivals and removed movies in {region_name}"):
            with st.spinner(f"Re-checking streaming availability changes for {region_name}..."):
                res = check_region_availability_changes(region_code, days=7)
                st.toast(f"{region_name}: {res['arrivals']} new arrivals, {res['left']} left, {res['switched']} switched", icon="🔄")
                st.success(f"Checked {res['checked']} movies for {region_name}: {res['arrivals']} new arrivals, {res['left']} left, {res['switched']} switched.")
                st.rerun()
        st.divider()
        mc, ac = count_db_global()
        st.caption(f"Global DB: {mc:,} movies, {ac:,} availability rows")
        st.caption(f"File: {get_db_path()} | Last sync {region_code}: {get_last_sync(region_code)}")
        if st.button("Force re-sync this region"):
            conn = get_connection()
            conn.execute("DELETE FROM sync_log WHERE region_code=?", (region_code,))
            conn.commit()
            conn.close()
            st.session_state.launch_sync_done = False
            st.rerun()

        with st.expander("❓ Help & Button Guide", expanded=False):
            st.markdown("""
            ### 🎛️ Selection Modes
            Controls how TMDB live discover orders results when fetching outside your local DB:
            - **Balanced 50/50**: Combines 50% recent releases with 50% popular movies.
            - **Recent**: Sorts primarily by newest release date.
            - **Popular**: Sorts primarily by highest popularity score.

            💡 **Can I skip/ignore Selection Mode?**  
            **Yes!** When searching or browsing your local DB (7,300+ titles), results are loaded instantly sorted by popularity. Selection mode is only used during fresh live discovery from TMDB API.

            ---

            ### 🔘 Sidebar Buttons Guide

            1. **Refresh TMDB results**
               - *What it does*: Clears in-memory UI caches and re-initializes detail queues.
               - *Speed*: **~1 second** (Instant).

            2. **Check arrivals & dropouts (FI/DK/IS)**
               - *What it does*: Queries TMDB for your selected region to detect new streaming additions, removed movies (dropouts), and provider switches, saving updates directly to `movies.sqlite`.
               - *Speed*: **~25–35 seconds** (batches ~100 network requests in parallel respecting TMDB API rate limits).

            3. **Force re-sync this region**
               - *What it does*: Resets the region sync log to trigger a fresh catalogue sync on launch.
               - *Speed*: **~5–10 seconds**.
            """)

    provider_ids = tuple(REGION_PROVIDERS.get(region_code, {}).get(n) for n in selected_service_names if n in REGION_PROVIDERS.get(region_code, {}))
    provider_ids = tuple(pid for pid in provider_ids if pid is not None)
    active_provider_ids: Tuple[int, ...] = provider_ids if streaming_only else tuple()

    # Launch sync once per session
    if not st.session_state.launch_sync_done:
        with st.spinner(f"Syncing global DB for {region_name}..."):
            added, up_to_date = run_launch_sync_global(region_code, active_provider_ids, streaming_only, genre_id, max_results, selection_mode)
            if added>0:
                st.toast(f"Global DB updated: {added} movies added/updated (shared across all regions)", icon="✅")
            elif up_to_date:
                st.toast(f"Global DB up to date ({mc:,} movies)", icon="👍")
        st.session_state.launch_sync_done = True

    st.title("Movie Catalogue")
    if streaming_only and selected_service_names:
        st.caption(f"Filtered to {', '.join(selected_service_names)} in {region_name}. Global DB mode - metadata shared.")
    else:
        st.caption(f"Showing all TMDB movies for {region_name}. Global DB mode.")

    # Try DB first with exact filters & tokenized search
    db_rows = fetch_from_global_db(
        region_code=region_code,
        genre_id=genre_id,
        max_results=max_results,
        search_text=title_search,
        streaming_only=streaming_only,
        selected_services=selected_service_names,
    )

    if title_search:
        if db_rows:
            movie_rows = db_rows
        else:
            # Local DB has 0 matches for this search term -> Try live TMDB search
            with st.spinner(f"Searching TMDB for '{title_search}'..."):
                search_rows = fetch_tmdb_search_movies(
                    region_code=region_code,
                    provider_ids=active_provider_ids,
                    genre_id=genre_id,
                    query=title_search,
                    max_results=min(max_results, TMDB_SEARCH_MAX_RESULTS),
                    streaming_only=streaming_only,
                )
            if search_rows:
                upsert_movies_global(search_rows)
                upsert_availability_batch([(r["movie_id"], region_code, "") for r in search_rows])
                if streaming_only and selected_service_names:
                    search_rows = [r for r in search_rows if movie_matches_selected_services(r["movie_id"], region_code, selected_service_names)]
            movie_rows = search_rows
    else:
        if db_rows:
            movie_rows = db_rows
        else:
            # Fallback live discover if DB is empty for this region
            with st.spinner(f"Fetching movies for {region_name} from TMDB (DB has 0 rows for this filter)..."):
                movie_rows = fetch_discover_movies(
                    region_code=region_code,
                    provider_ids=active_provider_ids,
                    genre_id=genre_id,
                    max_results=max_results,
                    streaming_only=streaming_only,
                    selection_mode=selection_mode,
                )
            if movie_rows:
                upsert_movies_global(movie_rows)
                upsert_availability_batch([(r["movie_id"], region_code, "") for r in movie_rows])

    movie_frame = pd.DataFrame(movie_rows)
    required_columns = {"movie_id","title","year","rating","votes","genres","overview"}
    if movie_frame.empty or not required_columns.issubset(movie_frame.columns):
        st.info("No movies match the current search or filters.")
        st.stop()

    movie_frame["year"] = pd.to_numeric(movie_frame["year"], errors="coerce")
    movie_frame["rating"] = pd.to_numeric(movie_frame["rating"], errors="coerce")
    movie_frame["votes"] = pd.to_numeric(movie_frame["votes"], errors="coerce")

    movie_rows = movie_frame.to_dict(orient="records")
    schedule_detail_jobs(movie_rows, region_code)
    drain_completed_detail_jobs()
    movie_rows = enrich_rows(movie_rows, region_code)
    movie_frame = pd.DataFrame(movie_rows)

    if movie_frame.empty:
        st.info("No movies match the current search text.")
        st.stop()

    if st.session_state.detail_complete:
        st.caption(f"Loaded {len(movie_frame):,} movies from global DB with full details.")
    else:
        st.caption(f"Loaded {len(movie_frame):,} movies. Detailed cast/director data will appear when enrichment finishes and will be cached globally.")
        detail_status_fragment(movie_rows, region_code)

    visible_columns = ["movie_id","title","year","rating","votes","genres","directors","actors","services"]
    grid_frame = movie_frame[visible_columns].copy()

    grid_builder = GridOptionsBuilder.from_dataframe(grid_frame)
    grid_builder.configure_column("movie_id", hide=True)
    grid_builder.configure_column("title", header_name="Title", filter="agTextColumnFilter", flex=3, minWidth=220)
    grid_builder.configure_column("year", header_name="Year", filter="agNumberColumnFilter", type=["numericColumn"], flex=1, minWidth=90)
    grid_builder.configure_column("rating", header_name="Rating", filter="agNumberColumnFilter", type=["numericColumn"], flex=1, minWidth=90)
    grid_builder.configure_column("votes", header_name="Votes", filter="agNumberColumnFilter", type=["numericColumn"], flex=1, minWidth=100)
    grid_builder.configure_column("genres", header_name="Genres", filter="agTextColumnFilter", flex=2, minWidth=180)
    grid_builder.configure_column("directors", header_name="Directors", filter="agTextColumnFilter", flex=2, minWidth=180)
    grid_builder.configure_column("actors", header_name="Actors", filter="agTextColumnFilter", flex=3, minWidth=220)
    grid_builder.configure_column("services", header_name="Services", filter="agTextColumnFilter", flex=2, minWidth=180)
    grid_builder.configure_default_column(resizable=True, sortable=True, floatingFilter=True)
    grid_builder.configure_selection(selection_mode="single", use_checkbox=False)
    grid_builder.configure_grid_options(rowHeight=32)
    grid_options = grid_builder.build()

    grid_response = AgGrid(grid_frame, gridOptions=grid_options, data_return_mode=DataReturnMode.FILTERED_AND_SORTED, update_mode=GridUpdateMode.SELECTION_CHANGED, columns_auto_size_mode=ColumnsAutoSizeMode.FIT_CONTENTS, height=680, theme="streamlit")

    selected_row = format_selected_row(grid_response.get("selected_rows"))
    if selected_row:
        selected_movie_id = selected_row.get("movie_id")
        dismissed_movie_id = st.session_state.get("dismissed_movie_id")
        if selected_movie_id is not None and st.session_state.get("selected_movie_id") != selected_movie_id and selected_movie_id != dismissed_movie_id:
            st.session_state["selected_movie_id"] = selected_movie_id
            st.session_state["selected_movie_title"] = selected_row.get("title")
            st.session_state["dismissed_movie_id"] = None
            st.rerun()

    active_movie_id = st.session_state.get("selected_movie_id")
    if active_movie_id is not None:
        row_lookup = movie_frame[movie_frame["movie_id"] == active_movie_id]
        row_data = {} if row_lookup.empty else row_lookup.iloc[0].to_dict()
        if not row_data:
            row_data = {"movie_id": active_movie_id, "title": st.session_state.get("selected_movie_title") or "Movie details"}
        detail_key_value = detail_key(int(active_movie_id), region_code)
        detail = st.session_state.detail_cache.get(detail_key_value)
        if detail is None:
            with st.spinner("Loading director and cast details..."):
                detail = fetch_movie_enrichment(int(active_movie_id), region_code)
                if detail:
                    st.session_state.detail_cache[detail_key_value] = detail
                    try:
                        update_movie_details_global(int(active_movie_id), ", ".join(detail.get("directors",[])), ", ".join(detail.get("cast",[])), detail.get("runtime"), detail.get("services",""), region_code)
                    except Exception:
                        pass

        def close_movie_dialog():
            st.session_state.dismissed_movie_id = active_movie_id
            st.session_state.selected_movie_id = None
            st.session_state.selected_movie_title = None

        @st.dialog(row_data.get("title") or "Movie details", width="large", on_dismiss=close_movie_dialog)
        def show_movie_dialog():
            poster_path = row_data.get("poster_path")
            left,right = st.columns([1,2])
            with left:
                if poster_path:
                    st.image(f"{TMDB_IMAGE_BASE}{poster_path}", width="stretch")
            with right:
                header_bits = []
                if row_data.get("year"):
                    header_bits.append(str(int(row_data["year"])))
                runtime = humanize_runtime(row_data.get("runtime"))
                if runtime:
                    header_bits.append(runtime)
                if row_data.get("rating") is not None:
                    header_bits.append(f"Rating {float(row_data['rating']):.1f}")
                if row_data.get("votes") is not None:
                    header_bits.append(f"{int(row_data['votes']):,} votes")
                if header_bits:
                    st.caption(" | ".join(header_bits))
                genres = row_data.get("genres")
                if genres:
                    st.markdown(f"**Genres:** {genres}")
                overview = row_data.get("overview")
                if overview:
                    st.write(overview)
                else:
                    st.caption("No overview available.")
                directors = (detail or {}).get("directors") or []
                if directors:
                    st.markdown(f"**Director:** {', '.join(directors)}")
                cast = (detail or {}).get("cast") or []
                if cast:
                    st.markdown(f"**Cast:** {', '.join(cast)}")
                st.divider()
                services = (detail or {}).get("services") or row_data.get("services") or ""
                if services:
                    st.markdown(f"**Availability in {region_name}:** {services}")
                else:
                    st.caption(f"No provider data available for {region_name} yet.")

        show_movie_dialog()

if __name__ == "__main__":
    main()
