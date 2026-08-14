import random
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
import streamlit as st
import streamlit.components.v1 as components

from app_config import DB_PATH, GENRES, REGION_PROVIDERS, REGIONS, get_secret
from letterboxd_profile import clean_title, parse_rating
from letterboxd_source_probe import probe_source

# TMDB API Setup
TMDB_API_KEY = get_secret("TMDB_API_KEY") or ""
TMDB_BASE_URL = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w500"

RANK_BATCH_SIZE = 24
LIST_BATCH_SIZE = 50
MATCH_POOL_SIZE = 120
MAX_MOVIE_PICKS = 5


# ==============================================================================
# LOCAL SQLITE DATABASE LAYER (movies.sqlite)
# ==============================================================================

def get_db_path() -> Path:
    return Path(DB_PATH)


def get_db_connection() -> sqlite3.Connection:
    db_path = get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db_schema() -> None:
    conn = get_db_connection()
    cur = conn.cursor()
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
    CREATE TABLE IF NOT EXISTS letterboxd_cache (
        letterboxd_slug TEXT PRIMARY KEY,
        tmdb_id INTEGER,
        title TEXT,
        year INTEGER,
        last_updated TEXT
    )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_movies_year ON movies(year)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_movies_pop ON movies(popularity DESC)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_avail_region ON availability(region_code)")
    conn.commit()
    conn.close()


init_db_schema()


def normalize_movie_record(movie: dict[str, Any]) -> dict[str, Any]:
    """Convert SQLite movie rows to the TMDB-shaped records used by the UI."""
    normalized = dict(movie)
    if "id" not in normalized and normalized.get("movie_id") is not None:
        normalized["id"] = normalized["movie_id"]
    if "vote_average" not in normalized and "rating" in normalized:
        normalized["vote_average"] = normalized["rating"]
    if "vote_count" not in normalized and "votes" in normalized:
        normalized["vote_count"] = normalized["votes"]
    return normalized


def upsert_movie_to_db(movie: dict[str, Any]) -> None:
    if not movie or not movie.get("id"):
        return
    conn = get_db_connection()
    cur = conn.cursor()
    now = datetime.now(timezone.utc).isoformat()
    release_date = movie.get("release_date") or ""
    year = int(release_date[:4]) if release_date and len(release_date) >= 4 and release_date[:4].isdigit() else None

    genres_val = movie.get("genres")
    if isinstance(genres_val, list):
        if genres_val and isinstance(genres_val[0], dict):
            genres_str = ", ".join([g.get("name", "") for g in genres_val])
        else:
            genres_str = ", ".join(map(str, genres_val))
    else:
        genres_str = str(genres_val or "")

    cur.execute("""
    INSERT INTO movies (movie_id, title, year, release_date, rating, votes, genres, overview, poster_path, popularity, last_updated)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(movie_id) DO UPDATE SET
        title = excluded.title,
        year = COALESCE(excluded.year, movies.year),
        release_date = COALESCE(excluded.release_date, movies.release_date),
        rating = COALESCE(excluded.rating, movies.rating),
        votes = COALESCE(excluded.votes, movies.votes),
        genres = COALESCE(excluded.genres, movies.genres),
        overview = COALESCE(excluded.overview, movies.overview),
        poster_path = COALESCE(excluded.poster_path, movies.poster_path),
        popularity = COALESCE(excluded.popularity, movies.popularity),
        last_updated = excluded.last_updated
    """, (
        movie["id"],
        movie.get("title") or "Untitled",
        year,
        release_date,
        movie.get("vote_average", 0.0),
        movie.get("vote_count", 0),
        genres_str,
        movie.get("overview", ""),
        movie.get("poster_path", ""),
        movie.get("popularity", 0.0),
        now,
    ))
    conn.commit()
    conn.close()


@st.cache_data(ttl=86400, show_spinner=False)
def resolve_letterboxd_item(title: str, year: Optional[int] = None, film_link: str = "") -> Optional[dict[str, Any]]:
    slug = film_link.strip().rstrip("/").split("/")[-1] if film_link else clean_title(title).lower().replace(" ", "-")
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT tmdb_id FROM letterboxd_cache WHERE letterboxd_slug=?", (slug,))
    row = cur.fetchone()
    if row and row["tmdb_id"]:
        tmdb_id = row["tmdb_id"]
        cur.execute("SELECT * FROM movies WHERE movie_id=?", (tmdb_id,))
        m_row = cur.fetchone()
        conn.close()
        if m_row:
            return normalize_movie_record(dict(m_row))

    params = {"api_key": TMDB_API_KEY, "query": title}
    if year:
        params["year"] = year

    try:
        res = requests.get(f"{TMDB_BASE_URL}/search/movie", params=params, timeout=10)
        if res.status_code == 200:
            results = res.json().get("results", [])
            if results:
                movie = results[0]
                tmdb_id = movie["id"]
                upsert_movie_to_db(movie)
                now = datetime.now(timezone.utc).isoformat()
                cur.execute("""
                INSERT INTO letterboxd_cache (letterboxd_slug, tmdb_id, title, year, last_updated)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(letterboxd_slug) DO UPDATE SET tmdb_id=excluded.tmdb_id, last_updated=excluded.last_updated
                """, (slug, tmdb_id, title, year, now))
                conn.commit()
                conn.close()
                return normalize_movie_record(movie)
    except Exception:
        pass

    conn.close()
    return None


# ==============================================================================
# GLOBAL ROOM SESSION STATE
# ==============================================================================

@st.cache_resource
def get_global_session() -> dict:
    return {
        "users": {},
        "genre": None,
        "movies": [],
        "movie_pool": [],
        "movie_cursor": 0,
        "mode": "rating",
        "state": "SETUP",
        "match": None,
        "region": "FI",
    }


lobby = get_global_session()


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def sort_movies_by_popularity(movies: List[dict]) -> List[dict]:
    return sorted(
        movies,
        key=lambda movie: (
            _safe_float(movie.get("popularity"), -1.0),
            _safe_float(movie.get("vote_count"), -1.0),
            _safe_float(movie.get("vote_average"), -1.0),
        ),
        reverse=True,
    )


def build_discover_params(genre_id: Optional[int], provider_ids: List[int], page: int, region: str = "FI") -> dict:
    params = {
        "api_key": TMDB_API_KEY,
        "language": "en-US",
        "watch_region": region,
        "region": region,
        "sort_by": "popularity.desc",
        "page": page,
    }
    if genre_id is not None:
        params["with_genres"] = genre_id
    if provider_ids:
        params["with_watch_providers"] = "|".join(map(str, provider_ids))
    return params


@st.cache_data(show_spinner=False, ttl=3600)
def fetch_ranked_movies(genre_id: Optional[int], provider_ids: tuple[int, ...], limit: int, region: str = "FI") -> List[dict]:
    movies: List[dict] = []
    page = 1
    total_pages: Optional[int] = None

    seen_ids: set[int] = set()
    while len(movies) < limit and (total_pages is None or page <= total_pages) and page <= 25:
        res = requests.get(f"{TMDB_BASE_URL}/discover/movie", params=build_discover_params(genre_id, list(provider_ids), page, region))
        if res.status_code != 200:
            break
        payload = res.json()
        results = payload.get("results", [])
        for movie in results:
            if movie["id"] not in seen_ids:
                seen_ids.add(movie["id"])
                movies.append(movie)
                upsert_movie_to_db(movie)
        total_pages = int(payload.get("total_pages") or page)
        if not results:
            break
        page += 1

    return sort_movies_by_popularity(movies)[:limit]


def get_combined_provider_ids() -> tuple[int, ...]:
    combined_services = set()
    for user in lobby["users"].values():
        combined_services.update(user.get("services", []))
    region_code = lobby.get("region", "FI")
    providers_map = REGION_PROVIDERS.get(region_code, REGION_PROVIDERS["FI"])
    return tuple(sorted(providers_map[service] for service in combined_services if service in providers_map))


def get_combined_service_names() -> List[str]:
    combined_services = set()
    for user in lobby["users"].values():
        combined_services.update(user.get("services", []))
    return sorted(combined_services)


def save_movie_availability(movie_id: int, region: str, services: List[str]) -> None:
    """Store the locally useful availability snapshot for the solo browser."""
    conn = get_db_connection()
    try:
        conn.execute(
            """
            INSERT INTO availability (movie_id, region_code, services, last_updated)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(movie_id, region_code) DO UPDATE SET
                services = excluded.services,
                last_updated = excluded.last_updated
            """,
            (movie_id, region, ", ".join(services), datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def get_local_movie_services(movie_id: int, region: str) -> List[str]:
    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT services FROM availability WHERE movie_id=? AND region_code=?",
            (movie_id, region),
        ).fetchone()
        return [service.strip() for service in (row["services"] if row else "").split(",") if service.strip()]
    finally:
        conn.close()


# ==============================================================================
# COMPOSITE CANDIDATE GENERATION ENGINE (Letterboxd + SQLite + TMDB)
# ==============================================================================

def generate_personalized_movie_pool(genre_name: Optional[str], limit: int = MATCH_POOL_SIZE) -> List[dict]:
    region = lobby.get("region", "FI")
    provider_ids = get_combined_provider_ids()
    genre_id = GENRES.get(genre_name) if genre_name else None

    item_scores: dict[int, float] = {}
    item_metadata: dict[int, dict] = {}
    watchlist_users: dict[int, set[str]] = {}
    high_rating_users: dict[int, set[str]] = {}

    for username, user_data in lobby["users"].items():
        lb = user_data.get("letterboxd")
        if not lb or not lb.get("summary"):
            continue

        for item in lb.get("watchlist", []):
            resolved = resolve_letterboxd_item(item["title"], item.get("year"), item.get("film_link"))
            if resolved:
                resolved = normalize_movie_record(resolved)
            if resolved and resolved.get("id") is not None:
                mid = resolved["id"]
                item_scores[mid] = item_scores.get(mid, 0.0) + 50.0
                item_metadata[mid] = resolved
                watchlist_users.setdefault(mid, set()).add(username)

        for item in lb.get("films", []):
            rating_val = item.get("rating")
            parsed_r = parse_rating(f"Title - {rating_val}") if isinstance(rating_val, str) else _safe_float(rating_val)
            if parsed_r and parsed_r >= 4.0:
                resolved = resolve_letterboxd_item(item["title"], item.get("year"), item.get("film_link"))
                if resolved:
                    resolved = normalize_movie_record(resolved)
                if resolved and resolved.get("id") is not None:
                    mid = resolved["id"]
                    item_scores[mid] = item_scores.get(mid, 0.0) + 25.0
                    item_metadata[mid] = resolved
                    high_rating_users.setdefault(mid, set()).add(username)

    base_discover = fetch_ranked_movies(genre_id, provider_ids, limit, region)
    combined_service_names = set(get_combined_service_names())
    pool_dict: dict[int, dict] = {}

    for movie in base_discover:
        mid = movie["id"]
        # Post-filter: confirm the movie is on at least one of the group's services
        providers = fetch_movie_watch_providers(mid, region)
        available = [p for p in providers if p in combined_service_names]
        if not available:
            continue
        base_score = _safe_float(movie.get("popularity"), 0.0) * 0.1
        score = item_scores.get(mid, 0.0) + base_score
        movie_copy = dict(movie)
        movie_copy["match_score"] = score
        movie_copy["watchlist_users"] = list(watchlist_users.get(mid, []))
        movie_copy["high_rating_users"] = list(high_rating_users.get(mid, []))
        movie_copy["available_on"] = available
        pool_dict[mid] = movie_copy

    for mid, movie in item_metadata.items():
        if mid not in pool_dict:
            # Only add Letterboxd items that are actually streamable by this group
            providers = fetch_movie_watch_providers(mid, region)
            available = [p for p in providers if p in combined_service_names]
            if not available:
                continue
            score = item_scores.get(mid, 0.0)
            movie_copy = dict(movie)
            movie_copy["match_score"] = score
            movie_copy["watchlist_users"] = list(watchlist_users.get(mid, []))
            movie_copy["high_rating_users"] = list(high_rating_users.get(mid, []))
            movie_copy["available_on"] = available
            pool_dict[mid] = movie_copy

    sorted_pool = sorted(pool_dict.values(), key=lambda m: m.get("match_score", 0.0), reverse=True)
    return sorted_pool[:limit]



def reset_lobby() -> None:
    lobby.clear()
    lobby.update(
        {
            "users": {},
            "genre": None,
            "movies": [],
            "movie_pool": [],
            "movie_cursor": 0,
            "mode": "rating",
            "state": "SETUP",
            "match": None,
            "final_results": [],
            "region": "FI",
        }
    )
    st.session_state.user_name = None
    for key in ("join_name", "join_services", "join_lb_username", "join_lb_consent", "reset_confirmation"):
        if key in st.session_state:
            del st.session_state[key]


def leave_lobby(user_name: str) -> None:
    lobby["users"].pop(user_name, None)
    st.session_state.user_name = None
    for key in ("join_name", "join_services", "join_lb_username", "join_lb_consent"):
        if key in st.session_state:
            del st.session_state[key]


def load_next_movie_batch() -> None:
    next_cursor = lobby.get("movie_cursor", 0) + RANK_BATCH_SIZE
    pool = lobby.get("movie_pool", [])
    if next_cursor + RANK_BATCH_SIZE > len(pool):
        pool = generate_personalized_movie_pool(lobby["genre"], max(MATCH_POOL_SIZE, next_cursor + RANK_BATCH_SIZE))
        lobby["movie_pool"] = pool
    lobby["movie_cursor"] = next_cursor
    lobby["movies"] = pool[next_cursor: next_cursor + RANK_BATCH_SIZE]
    for user in lobby["users"].values():
        user["votes"] = {}
        user["round2_votes"] = {}
        user["ready"] = False
        user["round2_ready"] = False
    lobby["state"] = "RATING"
    st.rerun()


def movie_batch_has_votes() -> bool:
    return any(
        any(user["votes"].get(movie["id"], 0) > 0 for user in lobby["users"].values())
        for movie in lobby["movies"]
    )


def start_matching(mode: str, genre_name: Optional[str]) -> bool:
    lobby["genre"] = genre_name
    lobby["movie_cursor"] = 0
    lobby["movie_pool"] = generate_personalized_movie_pool(genre_name, MATCH_POOL_SIZE)
    lobby["mode"] = mode
    lobby["movies"] = lobby["movie_pool"][:RANK_BATCH_SIZE]
    lobby["match"] = None
    lobby["final_results"] = []
    for user in lobby["users"].values():
        user["votes"] = {}
        user["round2_votes"] = {}
        user["ready"] = False
        user["round2_ready"] = False
    lobby["state"] = "RATING"
    if lobby["movies"]:
        st.rerun()
        return True
    return False


def start_browsing(genre_name: Optional[str]) -> bool:
    lobby["genre"] = genre_name
    lobby["movie_cursor"] = 0
    lobby["movie_pool"] = generate_personalized_movie_pool(genre_name, 96)
    lobby["state"] = "BROWSE"
    if lobby["movie_pool"]:
        st.rerun()
        return True
    return False


def load_prev_browse_page() -> None:
    curr_cursor = lobby.get("movie_cursor", 0)
    lobby["movie_cursor"] = max(0, curr_cursor - 96)
    st.rerun()


def load_next_browse_page() -> None:
    next_cursor = lobby.get("movie_cursor", 0) + 96
    pool = lobby.get("movie_pool", [])
    if next_cursor + 96 > len(pool):
        new_limit = len(pool) + 96
        pool = generate_personalized_movie_pool(lobby["genre"], new_limit)
        lobby["movie_pool"] = pool
    lobby["movie_cursor"] = next_cursor
    st.rerun()


def auto_refresh_page(interval_ms: int = 5000) -> None:
    components.html(
        f"""
        <script>
        setTimeout(function() {{
            const doc = window.parent.document;
            const buttons = Array.from(doc.querySelectorAll('button'));
            const refreshBtn = buttons.find(b => b.innerText.trim() === 'Auto Refresh' || b.innerText.includes('Auto Refresh'));
            if (refreshBtn) {{
                refreshBtn.click();
            }}
        }}, {interval_ms});
        </script>
        """,
        height=0,
        width=0,
    )


def current_movie_batch() -> List[dict]:
    pool = lobby.get("movie_pool", [])
    cursor = int(lobby.get("movie_cursor", 0) or 0)
    batch = pool[cursor: cursor + RANK_BATCH_SIZE]
    if batch:
        return batch
    return sort_movies_by_popularity(lobby.get("movies", []))


def toggle_movie_pick(movie_id: int, user_name: str) -> None:
    user = lobby["users"].get(user_name)
    if not user:
        return

    votes = user["votes"]
    if votes.get(movie_id, 0) > 0:
        votes.pop(movie_id, None)
        st.session_state.pop("movie_pick_limit_message", None)
        return

    picks_used = sum(1 for value in votes.values() if value > 0)
    if picks_used >= MAX_MOVIE_PICKS:
        st.session_state["movie_pick_limit_message"] = (
            "All five stars are in use. Deselect a movie before choosing another."
        )
        return

    votes[movie_id] = 1
    st.session_state.pop("movie_pick_limit_message", None)


FINAL_PICK_SCORES = (3, 2, 1)


def get_phase_two_movies() -> List[dict]:
    """Return every Phase 1 movie that received at least one pick."""
    interested_movies = []
    for movie in lobby.get("movies", []):
        interest_count = sum(
            1 for user in lobby["users"].values() if user["votes"].get(movie["id"], 0) > 0
        )
        if interest_count:
            movie_copy = dict(movie)
            movie_copy["interest_count"] = interest_count
            interested_movies.append(movie_copy)
    return sorted(
        interested_movies,
        key=lambda movie: (-movie["interest_count"], -_safe_float(movie.get("popularity"))),
    )


def set_final_movie_rank(movie_id: int, user_name: str, score: int) -> None:
    """Assign one unique final-vote star level to a movie, or toggle it off."""
    user = lobby["users"].get(user_name)
    if not user or score not in FINAL_PICK_SCORES or user.get("round2_ready"):
        return

    votes = user["round2_votes"]
    if votes.get(movie_id) == score:
        votes.pop(movie_id, None)
        return

    for other_movie_id, other_score in list(votes.items()):
        if other_score == score:
            votes.pop(other_movie_id, None)
    votes[movie_id] = score


def get_final_results(movies: List[dict]) -> List[dict]:
    """Return final-vote movies ranked by TMDB popularity for the results view."""
    results = []
    for movie in movies:
        final_stars = sum(
            _safe_float(user["round2_votes"].get(movie["id"], 0))
            for user in lobby["users"].values()
        )
        if final_stars > 0:
            results.append(
                {
                    "movie": movie,
                    "final_stars": final_stars,
                    "popularity": _safe_float(movie.get("popularity")),
                }
            )
    return sorted(results, key=lambda result: result["popularity"], reverse=True)


def choose_final_winner(results: List[dict]) -> Optional[dict]:
    if not results:
        return None
    return max(results, key=lambda result: (result["final_stars"], result["popularity"]))["movie"]


@st.cache_data(show_spinner=False, ttl=3600)
def fetch_movie_watch_providers(movie_id: int, region: str = "FI") -> List[str]:
    try:
        response = requests.get(f"{TMDB_BASE_URL}/movie/{movie_id}/watch/providers", params={"api_key": TMDB_API_KEY})
        if response.status_code != 200:
            return []
        payload = response.json().get("results", {}).get(region, {})
        provider_names: List[str] = []
        provider_id_to_name = {v: k for k, v in REGION_PROVIDERS.get(region, {}).items()}
        for bucket in ("flatrate", "free", "ads", "buy", "rent"):
            for provider in payload.get(bucket, []) or []:
                pid = provider.get("provider_id")
                name = provider_id_to_name.get(pid)
                if name and name not in provider_names:
                    provider_names.append(name)
        save_movie_availability(movie_id, region, provider_names)
        return provider_names
    except Exception:
        return []


st.set_page_config(page_title="WatchMatch", layout="wide", initial_sidebar_state="expanded")

# Standard Streamlit theme layout
st.markdown(
    """
    <style>
    section[data-testid="stSidebar"] div[data-testid="stButton"]:has(button[key*="hidden_"]),
    div[data-testid="stButton"]:has(button[aria-label*="Hidden"]),
    div[data-testid="stButton"]:has(button:contains("Hidden")) {
        display: none !important;
        height: 0px !important;
        margin: 0px !important;
        padding: 0px !important;
    }
    .badge-pill {
        display: inline-block;
        padding: 2px 8px;
        font-size: 0.75rem;
        font-weight: 600;
        border-radius: 12px;
        background-color: #2e3440;
        color: #d8dee9;
        margin-right: 4px;
        margin-bottom: 4px;
    }
    </style>
    """,
    unsafe_allow_html=True
)

components.html(
    """
    <script>
    function bindShortcuts(doc) {
        if (!doc) return;
        if (doc.window_watchmatch_keys_bound) return;

        const handleKey = function(e) {
            const targetTag = (e.target && e.target.tagName) ? e.target.tagName : "";
            if (targetTag === 'INPUT' || targetTag === 'TEXTAREA' || targetTag === 'SELECT') return;

            const key = (e.key || '').toLowerCase();
            const buttons = Array.from(doc.querySelectorAll('button'));
            let handled = false;

            if (key === ' ' || e.code === 'Space' || e.key === 'Spacebar') {
                const refreshBtn = buttons.find(b => b.innerText.trim() === 'Auto Refresh' || b.innerText.includes('Auto Refresh'));
                if (refreshBtn) {
                    refreshBtn.click();
                    handled = true;
                }
            } else if (key === 'h') {
                const hBtn = buttons.find(b => b.innerText.trim() === 'Help' || b.innerText.includes('Help'));
                if (hBtn) {
                    hBtn.click();
                    handled = true;
                }
            } else if (key === 'l') {
                const lBtn = buttons.find(b => b.innerText.includes('Movie List'));
                if (lBtn) {
                    lBtn.click();
                    handled = true;
                }
            } else if (key === 'r') {
                const rBtn = buttons.find(b => b.innerText.trim() === 'Reset' || b.innerText.includes('Reset'));
                if (rBtn) {
                    rBtn.click();
                    handled = true;
                }
            }

            if (handled) {
                e.preventDefault();
                e.stopPropagation();
            }
        };

        doc.addEventListener('keydown', handleKey, false);
        doc.window_watchmatch_keys_bound = true;
    }

    let parentDoc = null;
    try {
        if (window.parent && window.parent.document) {
            parentDoc = window.parent.document;
        }
    } catch (e) {}
    const localDoc = document;

    [parentDoc, localDoc].forEach(bindShortcuts);
    </script>
    """,
    height=0,
    width=0,
)


@st.dialog("Help & Instructions")
def show_help_dialog():
    st.markdown(
        """
    **Welcome to WatchMatch v2!**
    - **Step 1:** Enter your name, choose streaming services, and optionally provide your Letterboxd username & consent.
    - **Step 2:** Join the room and wait for your group of friends.
    - **Step 3:** Choose a group activity in Watch Party Setup, or select **Browse the local movie library** when you want to explore on your own.

    ### 🎬 Modes
    - **Group Match:** Personalized candidate pool combining Letterboxd watchlists, high ratings, and group streaming availability. Pick up to 5 movies with unique ranks 1–5, then vote Yes in Round 2!
    - **Local movie library:** Browse movies saved in `movies.sqlite`, select your availability region and streaming services, and see each saved movie's streaming services. It does not change an active watch party.

    ---
    *Keyboard Shortcuts:*
    - **H**: Show help menu.
    - **L**: Open ranked movie list.
    - **Spacebar**: Manual status refresh.
    """
    )
    st.info(
        "Group Match now uses five movie-pick stars instead of numeric ranks. "
        "Select a movie's empty star to use one; deselect its filled star to return it.",
        icon=":material/star:",
    )


@st.dialog("Ranked Movie List", width="large")
def show_movie_list_dialog():
    region = lobby.get("region", "FI")
    provider_ids = get_combined_provider_ids()
    sort_by = st.radio("Sort by", ["📈 Popularity", "⭐ TMDB Rating"], horizontal=True)

    with st.spinner("Fetching ranked movies and checking streaming services..."):
        try:
            genre_id = GENRES[lobby["genre"]] if lobby.get("genre") else None
            ranked_movies = fetch_ranked_movies(genre_id, provider_ids, LIST_BATCH_SIZE, region)
        except Exception:
            ranked_movies = []

        if not ranked_movies:
            st.info("No movies found for the selected criteria and streaming services.")
            return

        if "TMDB Rating" in sort_by:
            ranked_movies = sorted(ranked_movies, key=lambda m: _safe_float(m.get("vote_average"), -1), reverse=True)

        shared_services = get_combined_service_names()
        rows = []
        for rank, movie in enumerate(ranked_movies, start=1):
            providers = fetch_movie_watch_providers(movie["id"], region)
            available_on = [p for p in providers if p in shared_services]
            service = ", ".join(available_on) if available_on else "N/A"

            rows.append(
                {
                    "Rank": rank,
                    "Title": movie.get("title", "Untitled"),
                    "Streaming Service": service,
                    "Popularity": round(_safe_float(movie.get("popularity")), 2),
                    "TMDB Rating": movie.get("vote_average", "N/A"),
                    "Year": (movie.get("release_date", "") or "")[:4],
                }
            )

    sort_label = "TMDB Rating" if "TMDB Rating" in sort_by else "Popularity"
    genre_text = f"'{lobby['genre']}'" if lobby.get("genre") else "Any Genre"
    st.caption(f"Showing top movies sorted by {sort_label} for {genre_text} on shared streaming services.")
    st.dataframe(rows, width="stretch", hide_index=True)


@st.dialog("Reset WatchMatch")
def show_reset_dialog():
    st.warning("This clears all active users, votes, and the current match.")
    confirmation = st.text_input("Type reset to confirm", key="reset_confirmation")
    if st.button("Reset everything", type="primary"):
        if confirmation.strip().lower() == "reset":
            reset_lobby()
            st.rerun()
        else:
            st.error("Type reset exactly to confirm.")


with st.sidebar:
    with st.container():
        st.markdown('<div style="display: none;">', unsafe_allow_html=True)
        if st.button("Help", key="hidden_help_btn"):
            show_help_dialog()
        if st.button("Movie List", key="hidden_movie_list_btn"):
            show_movie_list_dialog()
        if st.button("Reset", key="hidden_reset_btn"):
            show_reset_dialog()
        if st.button("Auto Refresh", key="hidden_auto_refresh_btn"):
            st.rerun()
        st.markdown('</div>', unsafe_allow_html=True)

if st.session_state.get("user_name"):
    st.sidebar.divider()
    with st.sidebar.expander("🚪 Leave / Change Profile"):
        st.write("Leave the lobby to re-join with a different name or services.")
        if st.button("Leave Session", width="stretch", key="sidebar_leave_btn"):
            leave_lobby(st.session_state.user_name)
            st.rerun()

with st.sidebar.expander("⚠️ Reset Session"):
    st.write("This clears all active users, votes, and matches.")
    confirmation = st.text_input("Type 'reset' to confirm", key="sidebar_reset_confirmation", label_visibility="collapsed")
    if st.button("Reset Everything", type="primary", width="stretch", key="sidebar_reset_btn"):
        if confirmation.strip().lower() == "reset":
            reset_lobby()
            st.rerun()
        else:
            st.error("Type reset exactly to confirm.")

st.title("🍿 WatchMatch v2")
region_label = next((name for name, code in REGIONS.items() if code == lobby.get("region", "FI")), "your region")
st.markdown(f"Personalized movie selection powered by Letterboxd & local SQLite database ({region_label})")

refresh_col, menu_col, _ = st.columns([1.2, 1.8, 5])
with refresh_col:
    if st.button("🔄 Refresh", key="top_refresh_button"):
        st.rerun()
with menu_col:
    if st.button("📊 Ranked List", key="top_ranked_list_btn"):
        show_movie_list_dialog()

if "user_name" not in st.session_state:
    st.session_state.user_name = None
if "app_view" not in st.session_state:
    st.session_state.app_view = "group"

user_name = st.session_state.user_name

# Solo browsing is a separate activity, entered from the lobby or group setup.
# Keeping it out of the global header avoids making it look like part of every
# stage of a live group vote.
if st.session_state.app_view == "solo":
    if st.button("Back to watch party", icon=":material/arrow_back:", key="solo_return_to_group"):
        st.session_state.app_view = "group"
        st.rerun()

    st.subheader("Browse the local movie library", anchor=False)
    st.caption("Search movies already saved in movies.sqlite. This does not change your watch party.")

    region_names = list(REGIONS.keys())
    current_region_code = lobby.get("region", "FI")
    current_region_name = next(
        (name for name, code in REGIONS.items() if code == current_region_code),
        region_names[0],
    )
    col_search, col_genre, col_rating, col_region = st.columns([3, 2, 2, 2])
    with col_search:
        search_query = st.text_input("Search Title", placeholder="e.g. Inception").strip()
    with col_genre:
        browse_genre = st.selectbox("Genre Filter", ["All"] + [g for g in GENRES.keys() if g != "All"])
    with col_rating:
        min_rating = st.slider("Min TMDB Rating", 0.0, 10.0, 5.0, 0.5)
    with col_region:
        browse_region_name = st.selectbox(
            "Availability region",
            region_names,
            index=region_names.index(current_region_name),
            key="solo_browse_region",
        )
    browse_region = REGIONS[browse_region_name]
    browse_services = st.multiselect(
        "Services you have access to",
        list(REGION_PROVIDERS.get(browse_region, REGION_PROVIDERS["FI"]).keys()),
        key="solo_browse_services",
        placeholder="All saved services",
    )

    conn = get_db_connection()
    cur = conn.cursor()
    query = """
        SELECT movies.*, COALESCE(availability.services, '') AS available_on
        FROM movies
        LEFT JOIN availability
            ON availability.movie_id = movies.movie_id
            AND availability.region_code = ?
        WHERE movies.rating >= ?
    """
    params: list[Any] = [browse_region, min_rating]

    if search_query:
        query += " AND movies.title LIKE ?"
        params.append(f"%{search_query}%")
    if browse_genre != "All":
        query += " AND movies.genres LIKE ?"
        params.append(f"%{browse_genre}%")
    if browse_services:
        service_matchers = " OR ".join(
            "instr(',' || replace(availability.services, ', ', ',') || ',', ?) > 0"
            for _ in browse_services
        )
        query += f" AND ({service_matchers})"
        params.extend(f",{service}," for service in browse_services)

    query += " ORDER BY movies.popularity DESC LIMIT 48"
    cur.execute(query, params)
    sqlite_results = [dict(row) for row in cur.fetchall()]
    conn.close()

    if not sqlite_results:
        st.info("No matching movies found in local SQLite database. Try broadening your filter.")
    else:
        st.write(f"Showing **{len(sqlite_results)}** movies from `movies.sqlite`:")
        cols_per_row = 6
        for i in range(0, len(sqlite_results), cols_per_row):
            cols = st.columns(cols_per_row)
            for j in range(cols_per_row):
                if i + j < len(sqlite_results):
                    movie = sqlite_results[i + j]
                    with cols[j]:
                        with st.container(border=True):
                            if movie.get("poster_path"):
                                st.image(f"{TMDB_IMAGE_BASE}{movie['poster_path']}", width="stretch")
                            st.markdown(f"**{movie['title']}**")
                            services = movie.get("available_on", "")
                            if services:
                                st.caption(f"Watch on: {services}")
                            else:
                                st.caption(f"Availability not saved for {browse_region_name}")
                            st.caption(f"📅 {movie.get('year') or 'N/A'} · ⭐ {movie.get('rating', 'N/A')}")
                            with st.expander("ℹ️ Details"):
                                st.write(movie.get("overview", "No overview."))

    st.stop()


# GROUP MATCH FLOW
if not user_name:
    with st.container(border=True):
        st.subheader("Join the Watch Party")

        region_names = list(REGIONS.keys())
        current_region_code = lobby.get("region", "FI")
        current_region_name = next((k for k, v in REGIONS.items() if v == current_region_code), region_names[0])
        selected_region = st.selectbox("Region", region_names, index=region_names.index(current_region_name))
        lobby["region"] = REGIONS[selected_region]

        join_name = st.text_input("Your Name", key="join_name")

        current_providers = REGION_PROVIDERS.get(lobby["region"], REGION_PROVIDERS["FI"])
        join_services = st.multiselect(f"Your Streaming Services ({lobby['region']})", list(current_providers.keys()), key="join_services")

        st.divider()
        st.markdown("##### 🎬 Optional Letterboxd Personalization")
        lb_consent = st.checkbox("☑ Sync my Letterboxd taste & watchlist for group recommendations", value=True, key="join_lb_consent")
        lb_username = st.text_input("Letterboxd Username", placeholder="e.g. birgirm", key="join_lb_username").strip()

        if st.button("Join Room", type="primary", width="stretch"):
            join_name = join_name.strip()
            if join_name:
                lb_data = None
                if lb_consent and lb_username:
                    with st.spinner(f"Syncing public Letterboxd profile for {lb_username}..."):
                        try:
                            f_snap = probe_source(lb_username, "films")
                            w_snap = probe_source(lb_username, "watchlist")
                            rss_snap = probe_source(lb_username, "rss")

                            lb_data = {
                                "synced_at": datetime.now(timezone.utc).isoformat(),
                                "username": lb_username,
                                "summary": {
                                    "watched_count": len(f_snap.items),
                                    "watchlist_count": len(w_snap.items),
                                    "rss_count": len(rss_snap.items),
                                },
                                "films": f_snap.items,
                                "watchlist": w_snap.items,
                                "rss": rss_snap.items,
                            }
                        except Exception as exc:
                            st.warning(f"Letterboxd sync notice: {exc}")

                lobby["users"][join_name] = {
                    "services": join_services,
                    "votes": {},
                    "round2_votes": {},
                    "ready": False,
                    "round2_ready": False,
                    "letterboxd": lb_data,
                    "letterboxd_username": lb_username if lb_consent else None,
                }
                st.session_state.user_name = join_name
                st.rerun()
            else:
                st.error("Please enter your name to join.")

        st.divider()
        st.caption("Not joining a room right now?")
        if st.button(
            "Browse the local movie library",
            icon=":material/database:",
            key="join_solo_browser",
            width="stretch",
        ):
            st.session_state.app_view = "solo"
            st.rerun()
else:
    if lobby["match"]:
        st.success("🎉 IT'S A MATCH! 🎉")
        st.markdown(f"### You are all watching: **{lobby['match']['title']}** tonight!")
        if lobby["match"].get("poster_path"):
            st.image(f"{TMDB_IMAGE_BASE}{lobby['match']['poster_path']}", width=300)
        st.markdown(lobby["match"].get("overview", ""))

        available_on = lobby["match"].get("available_on") or []
        if not available_on:
            # Fallback for any edge case
            providers = fetch_movie_watch_providers(lobby["match"]["id"], lobby.get("region", "FI"))
            shared_services = set(get_combined_service_names())
            available_on = [p for p in providers if p in shared_services]
        if available_on:
            st.markdown(f"📺 **Watch on:** {', '.join(available_on)}")


        if st.button("Start Over / New Search"):
            reset_lobby()
            st.rerun()
        st.stop()

    if lobby["state"] == "SETUP":
        st.subheader("Watch Party Setup")
        st.markdown("Waiting for everyone to join...")
        auto_refresh_page()

        st.markdown("### Participants")
        combined_services = set()
        for name, data in lobby["users"].items():
            combined_services.update(data.get("services", []))

        # --- Participant summary table ---
        rows = []
        for name, data in lobby["users"].items():
            lb = data.get("letterboxd") or {}
            s = lb.get("summary", {})
            rows.append({
                "Name": name,
                "Services": ", ".join(data.get("services", [])) or "—",
                "Letterboxd": data.get("letterboxd_username") or "—",
                "Watched": s.get("watched_count", "—"),
                "Watchlist": s.get("watchlist_count", "—"),
            })
        st.dataframe(rows, hide_index=True, width="stretch")

        st.markdown(f"**Combined Services:** {', '.join(sorted(combined_services)) if combined_services else 'None'}")

        # --- Watchlist breakdown: one row per user ---
        watchlist_users = {
            name: data["letterboxd"]["watchlist"]
            for name, data in lobby["users"].items()
            if data.get("letterboxd") and data["letterboxd"].get("watchlist")
        }

        if watchlist_users:
            st.markdown("### Watchlists")
            for username, items in watchlist_users.items():
                with st.expander(f"🔖 {username} — {len(items)} movies", expanded=True):
                    wl_rows = []
                    for item in items:
                        wl_rows.append({
                            "Title": item.get("title", "—"),
                            "Year": item.get("year") or "—",
                            "Rating": item.get("rating") or "—",
                            "Liked": "❤️" if item.get("liked") else "",
                            "Link": item.get("film_link", ""),
                        })
                    st.dataframe(wl_rows, hide_index=True, width="stretch")

        if st.button("Refresh Participants"):
            st.rerun()

        st.divider()
        st.markdown("### Select Region, Genre & Start")

        genre_keys = list(GENRES.keys())
        genre_name = st.selectbox("Genre", genre_keys)
        effective_genre = None if genre_name == "All" else genre_name

        mode_col_a, mode_col_b = st.columns(2)
        with mode_col_a:
            if st.button("Start Group Match", type="primary", key="setup_movie_ranking_btn", width="stretch"):
                if not start_matching("rating", effective_genre):
                    st.error("No movies found for this combination of genre and streaming services.")
            st.caption("Pick up to five personalized movie choices.")
        with mode_col_b:
            if st.button("Group Browse Grid", type="secondary", key="setup_movie_list_btn", width="stretch"):
                if not start_browsing(effective_genre):
                    st.error("No movies found for this combination of genre and streaming services.")
            st.caption("Browse 96 movies at a time.")
        st.divider()
        if st.button(
            "Browse local library",
            icon=":material/database:",
            key="setup_solo_browser",
            width="stretch",
        ):
            st.session_state.app_view = "solo"
            st.rerun()
        st.caption("Explore saved movies without changing the group session.")

    elif lobby["state"] == "RATING":
        st.subheader(f"Genre: {lobby['genre'] or 'Any Genre'} - Phase 1: Pick your movies")
        st.markdown("Choose up to five movies. Select a star to pick it; select the filled star again to return it.")

        user_data = lobby["users"][user_name]
        movies_to_show = current_movie_batch()
        picks_used = sum(1 for value in user_data["votes"].values() if value > 0)
        stars_remaining = MAX_MOVIE_PICKS - picks_used

        with st.sidebar:
            with st.container(border=True):
                st.subheader("Your movie picks")
                st.markdown("★" * stars_remaining + "☆" * picks_used)
                st.caption(f"{stars_remaining} of {MAX_MOVIE_PICKS} stars remaining")

        if pick_limit_message := st.session_state.pop("movie_pick_limit_message", None):
            st.warning(pick_limit_message)

        if not movies_to_show:
            st.info("No movies found.")
            if st.button("Go Back to Setup"):
                lobby["state"] = "SETUP"
                st.rerun()
            st.stop()

        cols_per_row = 6
        for i in range(0, len(movies_to_show), cols_per_row):
            cols = st.columns(cols_per_row)
            for j in range(cols_per_row):
                if i + j < len(movies_to_show):
                    movie = movies_to_show[i + j]
                    with cols[j]:
                        with st.container(border=True):
                            rank_number = i + j + 1
                            st.caption(f"#{rank_number} · rating {_safe_float(movie.get('vote_average')):.1f}")
                            w_users = movie.get("watchlist_users", [])
                            if w_users:
                                st.markdown(f'<span class="badge-pill">🔖 Watchlist ({len(w_users)})</span>', unsafe_allow_html=True)
                            if movie.get("poster_path"):
                                st.image(f"{TMDB_IMAGE_BASE}{movie['poster_path']}", width="stretch")
                            else:
                                st.write("No poster")

                            is_picked = user_data["votes"].get(movie["id"], 0) > 0
                            if st.button(
                                "★" if is_picked else "☆",
                                key=f"pick_{movie['id']}",
                                help="Deselect this movie" if is_picked else "Pick this movie",
                            ):
                                toggle_movie_pick(movie["id"], user_name)
                                st.rerun()
                            with st.expander("ℹ️ Info"):
                                st.write(f"**{movie.get('title', 'Untitled')}** ({str(movie.get('release_date', ''))[:4]})")
                                st.write(movie.get("overview", "No overview."))

        st.divider()
        if user_data["ready"]:
            st.success("Waiting for other friends to finish choosing...")
            auto_refresh_page()
            if st.button("Refresh Status"):
                st.rerun()
        else:
            if st.button("Submit Picks", type="primary"):
                submitted_votes = [user_data["votes"].get(movie["id"], 0) for movie in movies_to_show]
                picked_movies = [vote for vote in submitted_votes if vote > 0]
                if not picked_movies:
                    st.error("Pick at least one movie before submitting.")
                elif len(picked_movies) > MAX_MOVIE_PICKS:
                    st.error("You can only pick up to five movies.")
                else:
                    lobby["users"][user_name]["ready"] = True

                    all_ready = all(u["ready"] for u in lobby["users"].values())
                    if all_ready:
                        if movie_batch_has_votes():
                            for user in lobby["users"].values():
                                user["round2_votes"] = {}
                                user["round2_ready"] = False
                            lobby["state"] = "ROUND_2"
                        else:
                            load_next_movie_batch()
                    st.rerun()

    elif lobby["state"] == "ROUND_2":
        st.subheader("Phase 2: Final choices")
        st.markdown(
            "Every movie that received a Phase 1 pick is here. Choose your most desired "
            "movie with **★★★**, next with **★★**, and third with **★**."
        )

        phase_two_movies = get_phase_two_movies()
        user_data = lobby["users"][user_name]
        required_scores = FINAL_PICK_SCORES[: min(len(phase_two_movies), len(FINAL_PICK_SCORES))]

        if not phase_two_movies:
            st.warning("Nobody picked a movie in Phase 1. Ready for a new batch?")
            col_a, col_b = st.columns(2)
            with col_a:
                if st.button("Load New Movies", type="primary"):
                    load_next_movie_batch()
            with col_b:
                if st.button("Start Over (Setup)"):
                    reset_lobby()
                    st.rerun()
            st.stop()

        movie_titles = {movie["id"]: movie.get("title", "Untitled") for movie in phase_two_movies}
        with st.sidebar:
            with st.container(border=True):
                st.subheader("Your final choices")
                for score in required_scores:
                    selected_movie_id = next(
                        (movie_id for movie_id, value in user_data["round2_votes"].items() if value == score),
                        None,
                    )
                    selected_title = movie_titles.get(selected_movie_id, "Not chosen yet")
                    st.markdown(f"{'★' * score} {selected_title}")
                st.caption("Select the same star level again to clear it.")

        if st.button("Refresh Status"):
            st.rerun()

        cols_per_row = 6
        for i in range(0, len(phase_two_movies), cols_per_row):
            cols = st.columns(cols_per_row)
            for j in range(cols_per_row):
                if i + j < len(phase_two_movies):
                    movie = phase_two_movies[i + j]
                    with cols[j]:
                        with st.container(border=True):
                            if movie.get("poster_path"):
                                st.image(f"{TMDB_IMAGE_BASE}{movie['poster_path']}", width="stretch")
                            else:
                                st.write("No poster")
                            st.markdown(f"**{movie.get('title', 'Untitled')}**")
                            st.caption(f"Picked by {movie['interest_count']} participant(s)")
                            with st.container(horizontal=True):
                                for score in required_scores:
                                    is_selected = user_data["round2_votes"].get(movie["id"]) == score
                                    if st.button(
                                        "★" * score,
                                        key=f"final_pick_{movie['id']}_{score}",
                                        type="primary" if is_selected else "secondary",
                                        disabled=user_data.get("round2_ready", False),
                                    ):
                                        set_final_movie_rank(movie["id"], user_name, score)
                                        st.rerun()
                            with st.expander("ℹ️ Details"):
                                st.write(f"**{movie['title']}** ({str(movie.get('release_date', ''))[:4]})")
                                st.write(movie.get("overview", "No overview."))

        st.divider()
        if user_data.get("round2_ready", False):
            st.success("Your final choices are in. Waiting for the others...")
            auto_refresh_page()
        elif st.button("Submit final choices", type="primary"):
            selected_scores = {
                score for score in user_data["round2_votes"].values() if score in required_scores
            }
            if selected_scores != set(required_scores):
                st.error("Use each available star level once before submitting.")
            else:
                user_data["round2_ready"] = True
                if all(user.get("round2_ready", False) for user in lobby["users"].values()):
                    final_results = get_final_results(phase_two_movies)
                    winner = choose_final_winner(final_results)
                    if winner:
                        lobby["final_results"] = final_results
                        lobby["match"] = winner
                st.rerun()

    elif lobby["state"] == "BROWSE":
        st.subheader(f"Genre: {lobby['genre'] or 'Any Genre'} - Browse Grid")
        cursor = lobby.get("movie_cursor", 0)
        movies_to_show = lobby.get("movie_pool", [])[cursor : cursor + 96]

        if not movies_to_show:
            st.info("No movies found.")
            if st.button("Go Back to Setup"):
                lobby["state"] = "SETUP"
                st.rerun()
            st.stop()

        cols_per_row = 6
        for i in range(0, len(movies_to_show), cols_per_row):
            cols = st.columns(cols_per_row)
            for j in range(cols_per_row):
                if i + j < len(movies_to_show):
                    movie = movies_to_show[i + j]
                    with cols[j]:
                        with st.container(border=True):
                            rank_num = cursor + i + j + 1
                            st.caption(f"#{rank_num}")
                            if movie.get("poster_path"):
                                st.image(f"{TMDB_IMAGE_BASE}{movie['poster_path']}", width="stretch")
                            st.markdown(f"**{movie.get('title', 'Untitled')}**")
                            st.caption(f"📅 {str(movie.get('release_date', ''))[:4]} · ⭐ {movie.get('vote_average', 'N/A')}")
                            with st.expander("ℹ️ Details"):
                                st.write(movie.get("overview", "No overview."))

        st.divider()
        nav_col1, nav_col2, nav_col3 = st.columns([1, 2, 1])
        with nav_col1:
            if cursor > 0:
                if st.button("⬅️ Previous Page", width="stretch"):
                    load_prev_browse_page()
        with nav_col2:
            if st.button("⚙️ Back to Setup", width="stretch"):
                lobby["state"] = "SETUP"
                st.rerun()
        with nav_col3:
            if st.button("Next Page ➡️", width="stretch"):
                load_next_browse_page()
