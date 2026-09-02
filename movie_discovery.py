"""Discovery engine -- live TMDB searches with service/region filtering.

Used by the Discover page inside watchmatch_app.py.
Intentionally self-contained: imports only stdlib + requests + app_config so it
is safe to import anywhere (does NOT call st.set_page_config or import streamlit).
"""

from __future__ import annotations

import concurrent.futures
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Literal, Optional, Sequence, Set

import requests

from app_config import DB_PATH, GENRES, REGION_PROVIDERS, get_secret

TMDB_API_KEY: str = get_secret("TMDB_API_KEY") or ""
TMDB_BASE_URL: str = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE: str = "https://image.tmdb.org/t/p/w342"
REQUEST_TIMEOUT: int = 10

_GENRE_ID_TO_NAME: Dict[int, str] = {v: k for k, v in GENRES.items() if v is not None}


def tmdb_get(path: str, params: Optional[Dict] = None) -> dict:
    payload: Dict = {"api_key": TMDB_API_KEY, "language": "en-US"}
    if params:
        payload.update(params)
    try:
        r = requests.get(f"{TMDB_BASE_URL}{path}", params=payload, timeout=REQUEST_TIMEOUT)
        return r.json() if r.status_code == 200 else {}
    except Exception:
        return {}


def provider_ids_for_services(region: str, services: Sequence[str]) -> List[int]:
    region_map = REGION_PROVIDERS.get(region, {})
    return [region_map[s] for s in services if s in region_map]


def _provider_pipe(region: str, services: Sequence[str]) -> Optional[str]:
    ids = provider_ids_for_services(region, services)
    return "|".join(str(i) for i in ids) if ids else None


def _get_conn() -> sqlite3.Connection:
    path = Path(DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def upsert_discovered_movies(movies: Sequence[dict]) -> None:
    if not movies:
        return
    conn = _get_conn()
    now = datetime.now(timezone.utc).isoformat()
    for m in movies:
        movie_id = m.get("id") or m.get("movie_id")
        if not movie_id:
            continue
        release_date = m.get("release_date") or ""
        year = m.get("year")
        if year is None and len(release_date) >= 4 and release_date[:4].isdigit():
            year = int(release_date[:4])
        genres_str = m.get("genres") or ""
        if not genres_str:
            genre_ids = m.get("genre_ids") or []
            genres_str = ", ".join(_GENRE_ID_TO_NAME[gid] for gid in genre_ids if gid in _GENRE_ID_TO_NAME)
        conn.execute(
            """
            INSERT INTO movies (movie_id, title, year, release_date, tmdb_rating, tmdb_votes,
                                genres, overview, poster_path, popularity, last_updated)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(movie_id) DO UPDATE SET
                title        = excluded.title,
                year         = COALESCE(excluded.year,         movies.year),
                release_date = COALESCE(NULLIF(excluded.release_date,''), movies.release_date),
                tmdb_rating  = COALESCE(excluded.tmdb_rating,  movies.tmdb_rating),
                tmdb_votes   = COALESCE(excluded.tmdb_votes,   movies.tmdb_votes),
                genres       = COALESCE(NULLIF(excluded.genres,''), movies.genres),
                overview     = COALESCE(NULLIF(excluded.overview,''), movies.overview),
                poster_path  = COALESCE(NULLIF(excluded.poster_path,''), movies.poster_path),
                popularity   = COALESCE(excluded.popularity,   movies.popularity),
                last_updated = excluded.last_updated
            """,
            (
                movie_id,
                m.get("title") or m.get("original_title") or "",
                year,
                release_date,
                round(float(m.get("vote_average") or m.get("tmdb_rating") or 0), 1),
                int(m.get("vote_count") or m.get("tmdb_votes") or 0),
                genres_str,
                m.get("overview") or "",
                m.get("poster_path") or "",
                float(m.get("popularity") or 0),
                now,
            ),
        )
    conn.commit()
    conn.close()


def search_person(name: str) -> List[dict]:
    data = tmdb_get("/search/person", {"query": name, "page": 1})
    return data.get("results", [])[:8]


def _normalise_movie(m: dict) -> dict:
    release_date = m.get("release_date") or ""
    genre_ids = m.get("genre_ids") or []
    genres = m.get("genres") or ", ".join(_GENRE_ID_TO_NAME[gid] for gid in genre_ids if gid in _GENRE_ID_TO_NAME)
    year_val = m.get("year")
    if year_val is None:
        year_val = int(release_date[:4]) if len(release_date) >= 4 and release_date[:4].isdigit() else None
    return {
        "id": m.get("id") or m.get("movie_id"),
        "movie_id": m.get("id") or m.get("movie_id"),
        "title": m.get("title") or m.get("original_title") or "",
        "release_date": release_date,
        "year": year_val,
        "poster_path": m.get("poster_path") or "",
        "popularity": float(m.get("popularity") or 0),
        "vote_average": round(float(m.get("vote_average") or m.get("tmdb_rating") or 0), 1),
        "vote_count": int(m.get("vote_count") or m.get("tmdb_votes") or 0),
        "overview": m.get("overview") or "",
        "genres": genres,
        "directors": m.get("directors") or "",
        "actors": m.get("actors") or "",
        "services": m.get("services") or "",
        "runtime": m.get("runtime"),
    }


def get_streaming_services_for_movie(movie_id: int, region: str) -> List[str]:
    region_map = REGION_PROVIDERS.get(region, {})
    id_to_name = {v: k for k, v in region_map.items()}
    data = tmdb_get(f"/movie/{movie_id}/watch/providers")
    region_prov = data.get("results", {}).get(region, {})
    names: List[str] = []
    for bucket in ("flatrate", "free", "ads"):
        for p in region_prov.get(bucket, []) or []:
            pid = p.get("provider_id")
            name = id_to_name.get(pid)
            if name and name not in names:
                names.append(name)
    return names


def fetch_movie_enrichment_tmdb(movie_id: int, region: str) -> dict:
    payload = tmdb_get(f"/movie/{movie_id}", {"append_to_response": "credits,watch/providers"})
    if not payload:
        return {}
    credits = payload.get("credits") or {}
    cast = [m.get("name") for m in credits.get("cast", [])[:10] if m and m.get("name")]
    directors = [m.get("name") for m in credits.get("crew", []) if m and m.get("job") == "Director" and m.get("name")]
    region_providers = payload.get("watch/providers", {}).get("results", {}).get(region, {})
    region_map = REGION_PROVIDERS.get(region, {})
    id_to_name = {v: k for k, v in region_map.items()}
    names: List[str] = []
    for bucket in ("flatrate", "free", "ads"):
        for p in region_providers.get(bucket, []) or []:
            name = id_to_name.get(p.get("provider_id"))
            if name and name not in names:
                names.append(name)
    return {
        "movie_id": movie_id,
        "cast": cast,
        "directors": directors,
        "services": ", ".join(names),
        "runtime": payload.get("runtime"),
    }


def filter_and_enrich_movies(
    movies: Sequence[dict],
    region: str,
    services: Sequence[str],
    require_services: bool = True,
    max_workers: int = 10,
) -> List[dict]:
    if not movies:
        return []

    target_services: Set[str] = set(services) if services else set(REGION_PROVIDERS.get(region, {}).keys())

    def check_one(m: dict) -> Optional[dict]:
        mid = m.get("id") or m.get("movie_id")
        if not mid:
            return None
        existing_svc_str = m.get("services") or ""
        matched: List[str] = []
        if existing_svc_str:
            item_svcs = [s.strip() for s in existing_svc_str.split(",") if s.strip()]
            matched = [s for s in item_svcs if s in target_services]
        else:
            svcs = get_streaming_services_for_movie(mid, region)
            matched = [s for s in svcs if s in target_services]

        if require_services and not matched:
            return None

        m_copy = dict(m)
        m_copy["id"] = mid
        m_copy["movie_id"] = mid
        m_copy["services"] = ", ".join(matched)
        return m_copy

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        enriched_results = list(executor.map(check_one, movies))

    filtered = [m for m in enriched_results if m is not None]
    return filtered


def discover_by_person(
    person_id: int,
    role: Literal["actor", "director"],
    region: str,
    services: Sequence[str],
    max_results: int = 50,
) -> List[dict]:
    key = "with_cast" if role == "actor" else "with_crew"
    active_services = list(services) if services else list(REGION_PROVIDERS.get(region, {}).keys())
    provider_str = _provider_pipe(region, active_services)

    params: dict = {
        key: person_id,
        "sort_by": "popularity.desc",
        "watch_region": region,
        "page": 1,
    }
    if provider_str:
        params["with_watch_providers"] = provider_str
        params["with_ott_monetization_types"] = "flatrate|free|ads"

    raw_movies: List[dict] = []
    page = 1
    while len(raw_movies) < max_results * 2 and page <= 5:
        params["page"] = page
        data = tmdb_get("/discover/movie", params)
        results = data.get("results") or []
        if not results:
            break
        raw_movies.extend(results)
        if page >= data.get("total_pages", 1):
            break
        page += 1

    normalised = [_normalise_movie(m) for m in raw_movies]
    filtered = filter_and_enrich_movies(normalised, region, active_services, require_services=True)
    trimmed = filtered[:max_results]
    upsert_discovered_movies(trimmed)
    return trimmed


def search_universal(
    query: str,
    region: str,
    services: Sequence[str],
    max_results: int = 50,
) -> List[dict]:
    """Single unified search field: matches actors, directors, movie titles, and descriptions.
    Enforces that results must be available on the selected/active streaming services in region.
    """
    if not query or not query.strip():
        return []

    q = query.strip()
    active_services = list(services) if services else list(REGION_PROVIDERS.get(region, {}).keys())

    # 1. Check local DB with tokenized search
    local_matches = fallback_local_search(q, region, active_services)
    if local_matches:
        # Local DB already has verified availability rows
        return local_matches[:max_results]

    # 2. Query TMDB: search person AND search movie simultaneously
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
        f_person = ex.submit(tmdb_get, "/search/person", {"query": q, "page": 1})
        f_movie = ex.submit(tmdb_get, "/search/movie", {"query": q, "page": 1, "region": region, "include_adult": "false"})

        person_data = f_person.result().get("results", []) or []
        movie_data = f_movie.result().get("results", []) or []

    combined_raw: Dict[int, dict] = {}
    for m in movie_data:
        mid = m.get("id")
        if mid:
            combined_raw[mid] = m

    # If top person found, fetch their movies (acting or directing)
    if person_data:
        top_person = person_data[0]
        pid = top_person.get("id")
        dept = (top_person.get("known_for_department") or "").lower()
        role_param = "with_crew" if dept == "directing" else "with_cast"

        prov_str = _provider_pipe(region, active_services)
        disc_params = {
            role_param: pid,
            "sort_by": "popularity.desc",
            "watch_region": region,
            "page": 1,
        }
        if prov_str:
            disc_params["with_watch_providers"] = prov_str
            disc_params["with_ott_monetization_types"] = "flatrate|free|ads"

        p_disc = tmdb_get("/discover/movie", disc_params)
        for m in p_disc.get("results", []) or []:
            mid = m.get("id")
            if mid and mid not in combined_raw:
                combined_raw[mid] = m

    if not combined_raw:
        return []

    normalised = [_normalise_movie(m) for m in combined_raw.values()]
    normalised.sort(key=lambda x: x["popularity"], reverse=True)

    # Strictly filter and enrich with streaming services
    filtered = filter_and_enrich_movies(normalised, region, active_services, require_services=True)
    trimmed = filtered[:max_results]
    upsert_discovered_movies(trimmed)
    return trimmed


def fallback_local_search(term: str, region: str, services: Sequence[str]) -> List[dict]:
    conn = _get_conn()
    active_services = list(services) if services else list(REGION_PROVIDERS.get(region, {}).keys())

    query = """
        SELECT m.movie_id, m.title, m.year, m.release_date, m.tmdb_rating, m.tmdb_votes,
               m.genres, m.overview, m.poster_path, m.popularity, m.runtime,
               m.directors, m.actors, COALESCE(a.services, '') AS services
        FROM movies m
        JOIN availability a ON m.movie_id = a.movie_id AND a.region_code = ?
        WHERE (a.services IS NOT NULL AND a.services != '')
    """
    params: list = [region]

    if term and term.strip():
        tokens = term.strip().split()
        for token in tokens:
            query += " AND (m.title LIKE ? OR m.genres LIKE ? OR m.directors LIKE ? OR m.actors LIKE ? OR m.overview LIKE ?)"
            pattern = f"%{token}%"
            params.extend([pattern] * 5)

    query += " ORDER BY m.popularity DESC"

    try:
        cur = conn.cursor()
        cur.execute(query, params)
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        return []

    if not rows:
        return []

    selected_set = set(active_services)

    def matches_services(svc_str: str) -> bool:
        if not svc_str:
            return False
        item_svcs = [s.strip() for s in svc_str.split(",") if s.strip()]
        return any(s in selected_set for s in item_svcs)

    matching = [r for r in rows if matches_services(r.get("services", ""))]
    normalised = [_normalise_movie(r) for r in matching]
    return normalised
