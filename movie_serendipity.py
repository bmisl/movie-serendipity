# serendipity_v3.py
import os
import random
import sqlite3
from collections import defaultdict
from typing import Dict, List, Optional, Sequence, Tuple

import requests
import streamlit as st

BASE_URL = "https://www.omdbapi.com/"
TMDB_BASE_URL = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w500"
DB_PATH = "movies.sqlite"

GENRES = [
    "Action",
    "Adventure",
    "Animation",
    "Comedy",
    "Crime",
    "Drama",
    "Family",
    "Fantasy",
    "History",
    "Horror",
    "Mystery",
    "Romance",
    "Sci-Fi",
    "Thriller",
    "War",
    "Western",
]

GENRE_DB_NAME_OVERRIDES: Dict[str, str] = {
    "Sci-Fi": "Science Fiction",
}

DB_GENRE_TO_UI = {value: key for key, value in GENRE_DB_NAME_OVERRIDES.items()}

TMDB_GENRE_IDS: Dict[str, int] = {
    "Action": 28,
    "Adventure": 12,
    "Animation": 16,
    "Comedy": 35,
    "Crime": 80,
    "Drama": 18,
    "Family": 10751,
    "Fantasy": 14,
    "History": 36,
    "Horror": 27,
    "Mystery": 9648,
    "Romance": 10749,
    "Sci-Fi": 878,
    "Thriller": 53,
    "War": 10752,
    "Western": 37,
}

def get_secret(key: str) -> Optional[str]:
    """Fetch configuration values from Streamlit secrets or the environment."""

    if hasattr(st, "secrets") and key in st.secrets:
        return st.secrets[key]
    return os.getenv(key)


OMDB_API_KEY = get_secret("OMDB_API_KEY")
TMDB_API_KEY = get_secret("TMDB_API_KEY")


def ensure_api_key(key: Optional[str], label: str) -> str:
    """Show a helpful error if a required API key is missing."""

    if not key:
        st.error(
            f"Missing {label}. Add it to Streamlit secrets or as an environment variable "
            f"named {label}."
        )
        st.stop()
    return key


st.set_page_config(layout="wide")
st.title("🎬 Serendipitous Movie Picker")

ensure_api_key(OMDB_API_KEY, "OMDB_API_KEY")
ensure_api_key(TMDB_API_KEY, "TMDB_API_KEY")


def tmdb_get(path: str, params: Optional[Dict[str, object]] = None) -> Optional[dict]:
    """Perform a TMDB API request and gracefully handle errors."""

    merged: Dict[str, object] = {"api_key": TMDB_API_KEY, "language": "en-US"}
    if params:
        merged.update(params)

    try:
        response = requests.get(
            f"{TMDB_BASE_URL}/{path}",
            params=merged,
            timeout=10,
        )
        response.raise_for_status()
        return response.json()
    except requests.RequestException:
        st.error("Unable to communicate with TMDB right now. Please try again later.")
        return None


@st.cache_data(show_spinner=False)
def fetch_tmdb_movie_detail(movie_id: int) -> Optional[dict]:
    """Retrieve TMDB movie information, including credits and IMDb IDs."""

    payload = tmdb_get(
        f"movie/{movie_id}",
        params={"append_to_response": "credits,external_ids"},
    )
    if not payload:
        return None

    credits = payload.get("credits", {})
    directors = [
        member.get("name")
        for member in credits.get("crew", [])
        if member.get("job") == "Director" and member.get("name")
    ]
    cast = [
        member.get("name")
        for member in credits.get("cast", [])
        if member.get("name")
    ]

    external_ids = payload.get("external_ids", {})
    imdb_id = external_ids.get("imdb_id")

    poster_path = payload.get("poster_path")
    poster_url = f"{TMDB_IMAGE_BASE}{poster_path}" if poster_path else None
    release_date = payload.get("release_date") or ""
    release_year = release_date[:4] if release_date else ""

    runtime = payload.get("runtime")
    runtime_text = f"{runtime} min" if isinstance(runtime, int) and runtime > 0 else ""

    return {
        "tmdb_id": movie_id,
        "title": payload.get("title") or payload.get("name") or "Unknown Title",
        "release_year": release_year,
        "directors": sorted({name for name in directors if name}),
        "actors": [name for name in cast[:20] if name],
        "imdb_id": imdb_id,
        "vote_average": float(payload.get("vote_average") or 0.0),
        "overview": payload.get("overview", ""),
        "poster_url": poster_url,
        "runtime_text": runtime_text,
    }


@st.cache_data(show_spinner=False)
def load_available_genres() -> List[str]:
    """Return the genres present in the local database, falling back to defaults."""

    try:
        conn = sqlite3.connect(DB_PATH)
    except sqlite3.Error:
        return GENRES

    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='movie_genres'"
        )
        if not cur.fetchone():
            return GENRES

        cur.execute("SELECT DISTINCT genre FROM movie_genres ORDER BY genre")
        rows = [row[0] for row in cur.fetchall() if row[0]]
    finally:
        conn.close()

    if not rows:
        return GENRES

    ui_genres: List[str] = []
    for genre_name in rows:
        ui_name = DB_GENRE_TO_UI.get(genre_name, genre_name)
        if ui_name in TMDB_GENRE_IDS and ui_name not in ui_genres:
            ui_genres.append(ui_name)

    return ui_genres or GENRES


def ui_to_db_genre(genre: str) -> str:
    """Translate the UI representation of a genre to its database value."""

    return GENRE_DB_NAME_OVERRIDES.get(genre, genre)


def get_connection() -> sqlite3.Connection:
    """Open a connection to the local SQLite database with helpful messaging."""

    try:
        return sqlite3.connect(DB_PATH)
    except sqlite3.Error:
        st.error("Unable to open the local movie database. Ensure movies.sqlite exists.")
        raise


def movie_table_has_column(column: str) -> bool:
    """Return True when the movies table includes the requested column."""

    try:
        conn = get_connection()
    except sqlite3.Error:
        return False

    try:
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(movies)")
        return any(row[1] == column for row in cur.fetchall())
    finally:
        conn.close()


@st.cache_data(show_spinner=False)
def load_directors_for_genres(genres: Tuple[str, ...]) -> List[str]:
    """Return directors who have movies within the provided genres."""

    db_genres = tuple(ui_to_db_genre(genre) for genre in genres if genre)

    try:
        conn = get_connection()
    except sqlite3.Error:
        return []

    try:
        cur = conn.cursor()
        params: List[object] = []
        filters: List[str] = []
        if db_genres:
            placeholders = ",".join("?" for _ in db_genres)
            filters.append(
                f"EXISTS (SELECT 1 FROM movie_genres mg WHERE mg.movie_id = m.id AND mg.genre IN ({placeholders}))"
            )
            params.extend(db_genres)

        where_clause = " AND ".join(filters)
        if where_clause:
            where_clause = f"WHERE {where_clause}"

        query = f"""
            SELECT DISTINCT p.name
            FROM movies m
            JOIN movie_people mp ON mp.movie_id = m.id AND mp.role = 'Director'
            JOIN people p ON p.id = mp.person_id
            {where_clause}
            ORDER BY p.name
        """
        cur.execute(query, params)
        return [row[0] for row in cur.fetchall() if row[0]]
    finally:
        conn.close()


@st.cache_data(show_spinner=False)
def load_actors_for_filters(
    genres: Tuple[str, ...],
    directors: Tuple[str, ...],
) -> List[str]:
    """Return actors who appear in movies matching the provided filters."""

    db_genres = tuple(ui_to_db_genre(genre) for genre in genres if genre)
    director_names = tuple(director for director in directors if director)

    try:
        conn = get_connection()
    except sqlite3.Error:
        return []

    try:
        cur = conn.cursor()
        filters: List[str] = []
        params: List[object] = []

        if db_genres:
            placeholders = ",".join("?" for _ in db_genres)
            filters.append(
                f"EXISTS (SELECT 1 FROM movie_genres mg WHERE mg.movie_id = m.id AND mg.genre IN ({placeholders}))"
            )
            params.extend(db_genres)

        if director_names:
            placeholders = ",".join("?" for _ in director_names)
            filters.append(
                f"EXISTS ("
                "    SELECT 1"
                "    FROM movie_people md"
                "    JOIN people pd ON pd.id = md.person_id"
                "    WHERE md.movie_id = m.id AND md.role = 'Director' AND pd.name IN ("
                f"{placeholders}"
                ")"
            )
            params.extend(director_names)

        where_clause = " AND ".join(filters)
        if where_clause:
            where_clause = f"WHERE {where_clause}"

        query = f"""
            SELECT DISTINCT p.name
            FROM movies m
            JOIN movie_people mp ON mp.movie_id = m.id AND mp.role = 'Actor'
            JOIN people p ON p.id = mp.person_id
            {where_clause}
            ORDER BY p.name
        """
        cur.execute(query, params)
        return [row[0] for row in cur.fetchall() if row[0]]
    finally:
        conn.close()


def collect_people_for_movies(movie_ids: Sequence[int]) -> Tuple[Dict[int, List[str]], Dict[int, List[str]]]:
    """Load directors and actors for the provided movie identifiers."""

    if not movie_ids:
        return {}, {}

    try:
        conn = get_connection()
    except sqlite3.Error:
        return {}, {}

    directors_map: Dict[int, List[str]] = {movie_id: [] for movie_id in movie_ids}
    actors_map: Dict[int, List[str]] = {movie_id: [] for movie_id in movie_ids}

    try:
        cur = conn.cursor()
        placeholders = ",".join("?" for _ in movie_ids)

        cur.execute(
            f"""
            SELECT mp.movie_id, p.name
            FROM movie_people mp
            JOIN people p ON p.id = mp.person_id
            WHERE mp.movie_id IN ({placeholders}) AND mp.role = 'Director'
            ORDER BY p.name
        """,
            movie_ids,
        )
        for movie_id, name in cur.fetchall():
            if name:
                directors_map[movie_id].append(name)

        cur.execute(
            f"""
            SELECT mp.movie_id, p.name
            FROM movie_people mp
            JOIN people p ON p.id = mp.person_id
            WHERE mp.movie_id IN ({placeholders}) AND mp.role = 'Actor'
            ORDER BY mp.rowid
        """,
            movie_ids,
        )
        for movie_id, name in cur.fetchall():
            if name and len(actors_map[movie_id]) < 20:
                actors_map[movie_id].append(name)

    finally:
        conn.close()

    return directors_map, actors_map


def collect_genres_for_movies(movie_ids: Sequence[int]) -> Dict[int, List[str]]:
    """Return the genres associated with each provided movie identifier."""

    if not movie_ids:
        return {}

    try:
        conn = get_connection()
    except sqlite3.Error:
        return {}

    genres_map: Dict[int, List[str]] = defaultdict(list)

    try:
        cur = conn.cursor()
        placeholders = ",".join("?" for _ in movie_ids)
        cur.execute(
            f"SELECT movie_id, genre FROM movie_genres WHERE movie_id IN ({placeholders})",
            movie_ids,
        )
        for movie_id, genre in cur.fetchall():
            if not genre:
                continue
            ui_genre = DB_GENRE_TO_UI.get(genre, genre)
            if ui_genre not in genres_map[movie_id]:
                genres_map[movie_id].append(ui_genre)
    finally:
        conn.close()

    for values in genres_map.values():
        values.sort()

    return genres_map


@st.cache_data(show_spinner=False)
def fetch_movies_for_filters(
    genres: Tuple[str, ...],
    directors: Tuple[str, ...],
    actors: Tuple[str, ...],
    limit: int = 200,
    include_poster_path: Optional[bool] = None,
) -> List[dict]:
    """Retrieve candidate movies scored by how well they match the filters."""

    db_genres = tuple(ui_to_db_genre(genre) for genre in genres if genre)
    director_names = tuple(director for director in directors if director)
    actor_names = tuple(actor for actor in actors if actor)

    try:
        conn = get_connection()
    except sqlite3.Error:
        return []

    try:
        cur = conn.cursor()

        if include_poster_path is None:
            include_poster_path = movie_table_has_column("poster_path")

        poster_select = "m.poster_path" if include_poster_path else "NULL"

        if not db_genres and not director_names and not actor_names:
            cur.execute(
                f"""
                SELECT
                    m.id,
                    m.title,
                    m.year,
                    m.runtime,
                    m.vote_average,
                    m.overview,
                    m.popularity,
                    {poster_select} AS poster_path
                FROM movies m
                ORDER BY RANDOM()
                LIMIT ?
                """,
                (limit,),
            )
            rows = cur.fetchall()
            movie_ids = [row[0] for row in rows]
            directors_map, actors_map = collect_people_for_movies(movie_ids)
            genres_map = collect_genres_for_movies(movie_ids)
            return [
                build_movie_payload(row, directors_map, actors_map, genres_map, 0, 0, 0)
                for row in rows
            ]

        params: List[object] = []

        genre_match_expr = "0"
        director_match_expr = "0"
        actor_match_expr = "0"
        filters: List[str] = []

        if db_genres:
            placeholders = ",".join("?" for _ in db_genres)
            genre_match_expr = (
                "(SELECT COUNT(*) FROM movie_genres mg WHERE mg.movie_id = m.id AND mg.genre IN ("
                f"{placeholders}"
                "))"
            )
            params.extend(db_genres)
            filters.append(
                f"EXISTS (SELECT 1 FROM movie_genres mg WHERE mg.movie_id = m.id AND mg.genre IN ({placeholders}))"
            )
            params.extend(db_genres)

        if director_names:
            placeholders = ",".join("?" for _ in director_names)
            director_match_expr = (
                "(SELECT COUNT(*) FROM movie_people md "
                "JOIN people pd ON pd.id = md.person_id "
                "WHERE md.movie_id = m.id AND md.role = 'Director' AND pd.name IN ("
                f"{placeholders}"
                "))"
            )
            params.extend(director_names)
            filters.append(
                "EXISTS (SELECT 1 FROM movie_people md JOIN people pd ON pd.id = md.person_id "
                "WHERE md.movie_id = m.id AND md.role = 'Director' AND pd.name IN ("
                f"{placeholders}"
                "))"
            )
            params.extend(director_names)

        if actor_names:
            placeholders = ",".join("?" for _ in actor_names)
            actor_match_expr = (
                "(SELECT COUNT(*) FROM movie_people ma "
                "JOIN people pa ON pa.id = ma.person_id "
                "WHERE ma.movie_id = m.id AND ma.role = 'Actor' AND pa.name IN ("
                f"{placeholders}"
                "))"
            )
            params.extend(actor_names)
            filters.append(
                "EXISTS (SELECT 1 FROM movie_people ma JOIN people pa ON pa.id = ma.person_id "
                "WHERE ma.movie_id = m.id AND ma.role = 'Actor' AND pa.name IN ("
                f"{placeholders}"
                "))"
            )
            params.extend(actor_names)

        where_clause = " OR ".join(filters)
        query = f"""
            SELECT
                m.id,
                m.title,
                m.year,
                m.runtime,
                m.vote_average,
                m.overview,
                m.popularity,
                {poster_select} AS poster_path,
                {genre_match_expr} AS genre_matches,
                {director_match_expr} AS director_matches,
                {actor_match_expr} AS actor_matches
            FROM movies m
            WHERE {where_clause}
            ORDER BY (genre_matches * 2 + director_matches * 4 + actor_matches * 3) DESC,
                     m.vote_average DESC,
                     m.popularity DESC
            LIMIT ?
        """
        cur.execute(query, (*params, limit))
        rows = cur.fetchall()
        if not rows:
            return []

        movie_ids = [row[0] for row in rows]
        directors_map, actors_map = collect_people_for_movies(movie_ids)
        genres_map = collect_genres_for_movies(movie_ids)
        return [
            build_movie_payload(
                row,
                directors_map,
                actors_map,
                genres_map,
                row[8],
                row[9],
                row[10],
            )
            for row in rows
        ]
    finally:
        conn.close()


def build_movie_payload(
    row: Tuple,
    directors_map: Dict[int, List[str]],
    actors_map: Dict[int, List[str]],
    genres_map: Dict[int, List[str]],
    genre_matches: int,
    director_matches: int,
    actor_matches: int,
) -> dict:
    """Construct a movie payload with standardised fields and scoring metadata."""

    movie_id, title, year, runtime, vote_average, overview, _popularity, poster_path = row[:8]
    release_year = str(year) if year else ""
    runtime_text = (
        f"{runtime} min" if isinstance(runtime, int) and runtime and runtime > 0 else ""
    )
    return {
        "tmdb_id": movie_id,
        "title": title or "Unknown Title",
        "release_year": release_year,
        "directors": directors_map.get(movie_id, []),
        "actors": actors_map.get(movie_id, []),
        "vote_average": float(vote_average or 0.0),
        "overview": overview or "",
        "poster_path": poster_path,
        "poster_url": f"{TMDB_IMAGE_BASE}{poster_path}" if poster_path else None,
        "runtime_text": runtime_text,
        "genre_matches": int(genre_matches or 0),
        "director_matches": int(director_matches or 0),
        "actor_matches": int(actor_matches or 0),
        "genres": genres_map.get(movie_id, []),
    }


@st.cache_data(show_spinner=False)
def fetch_omdb_movie_detail(imdb_id: str) -> Optional[dict]:
    """Retrieve detailed OMDb information for a single movie."""

    try:
        detail_response = requests.get(
            BASE_URL,
            params={"i": imdb_id, "apikey": OMDB_API_KEY},
            timeout=10,
        )
        detail_response.raise_for_status()
        detail = detail_response.json()
    except requests.RequestException:
        st.error("Couldn't load full movie details right now.")
        return None

    if detail.get("Response") != "True":
        st.warning("Movie details are currently unavailable.")
        return None
    if detail.get("Type", "").lower() != "movie":
        return None
    return detail


def parse_csv_list(value: Optional[str]) -> List[str]:
    """Split a comma-separated string into a cleaned list of values."""

    if not value:
        return []
    return [item.strip() for item in value.split(",") if item and item.strip()]


def combine_unique_values(primary: Sequence[str], secondary: Sequence[str]) -> List[str]:
    """Merge two sequences into a list without duplicates, preserving order."""

    seen = set()
    combined: List[str] = []

    for source in (primary, secondary):
        for item in source:
            if not item:
                continue
            key = item.strip()
            if not key:
                continue
            lowered = key.lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            combined.append(key)

    return combined


def normalise_genre_name(name: str) -> Optional[str]:
    """Convert a raw genre name to one of the UI filter labels when possible."""

    cleaned = name.strip()
    if not cleaned:
        return None
    if cleaned in TMDB_GENRE_IDS:
        return cleaned
    if cleaned in GENRES:
        return cleaned
    if cleaned in DB_GENRE_TO_UI:
        return DB_GENRE_TO_UI[cleaned]
    return None


def trigger_rerun() -> None:
    """Request a Streamlit rerun using the supported API for the current version."""

    if hasattr(st, "rerun"):
        st.rerun()
    elif hasattr(st, "experimental_rerun"):
        st.experimental_rerun()


def add_filter_value(session_key: str, value: str) -> None:
    """Append a value to a session-based filter list if it is not already present."""

    selections = st.session_state.get(session_key, [])
    if value in selections:
        return
    st.session_state[session_key] = [*selections, value]
    st.session_state["current_movie_id"] = None
    trigger_rerun()


def render_filter_chips(
    label: str,
    chips: Sequence[str],
    session_key: str,
    prefix: str,
) -> bool:
    """Render clickable chips that add their value to the corresponding filters."""

    values = [chip for chip in chips if chip]
    if not values:
        return False

    st.markdown(f"**{label}:**")
    columns = st.columns(min(len(values), 4))
    for index, value in enumerate(values):
        column = columns[index % len(columns)]
        with column:
            key = make_checkbox_key(f"{prefix}_chip", value)
            if st.button(value, key=key, use_container_width=True):
                add_filter_value(session_key, value)
    if not st.session_state.get("_chips_caption_shown"):
        st.caption("Tap a value to refine your filters.")
        st.session_state["_chips_caption_shown"] = True
    return True


def render_movie_detail(movie: dict, omdb_detail: Optional[dict]) -> None:
    """Display details for the selected movie using OMDb data with TMDB fallbacks."""

    poster_url: Optional[str] = None
    if omdb_detail and omdb_detail.get("Poster") and omdb_detail["Poster"] != "N/A":
        poster_url = omdb_detail["Poster"]
    elif movie.get("poster_url"):
        poster_url = movie["poster_url"]

    if poster_url:
        st.image(poster_url, width=260)

    title = (
        omdb_detail.get("Title")
        if omdb_detail and omdb_detail.get("Title")
        else movie.get("title", "Unknown Title")
    )
    year = (
        omdb_detail.get("Year")
        if omdb_detail and omdb_detail.get("Year")
        else movie.get("release_year", "N/A")
    )

    rating_value: Optional[str]
    if omdb_detail and omdb_detail.get("imdbRating") and omdb_detail["imdbRating"] != "N/A":
        rating_value = omdb_detail["imdbRating"]
    elif movie.get("vote_average"):
        rating_value = f"{movie['vote_average']:.1f}"
    else:
        rating_value = "N/A"

    st.markdown(f"**{title} ({year})** ⭐ {rating_value}")

    runtime = ""
    if omdb_detail and omdb_detail.get("Runtime") and omdb_detail["Runtime"] != "N/A":
        runtime = omdb_detail["Runtime"]
    elif movie.get("runtime_text"):
        runtime = movie["runtime_text"]

    director_values = combine_unique_values(
        movie.get("directors", []),
        parse_csv_list(omdb_detail.get("Director")) if omdb_detail else [],
    )
    actor_values = combine_unique_values(
        movie.get("actors", []),
        parse_csv_list(omdb_detail.get("Actors")) if omdb_detail else [],
    )
    genre_values = combine_unique_values(
        movie.get("genres", []),
        parse_csv_list(omdb_detail.get("Genre")) if omdb_detail else [],
    )

    recognised_genres: List[str] = []
    display_only_genres: List[str] = []
    for name in genre_values:
        ui_name = normalise_genre_name(name)
        if ui_name:
            if ui_name not in recognised_genres:
                recognised_genres.append(ui_name)
        elif name not in display_only_genres:
            display_only_genres.append(name)

    if not render_filter_chips("Genres", recognised_genres, "selected_genres", "genre"):
        if genre_values:
            st.markdown(f"**Genres:** {', '.join(genre_values)}")
    elif display_only_genres:
        st.caption(", ".join(display_only_genres))

    if not render_filter_chips("Directors", director_values, "selected_directors", "director"):
        if director_values:
            st.markdown(f"**Director:** {', '.join(director_values)}")

    if not render_filter_chips("Actors", actor_values[:10], "selected_actors", "actor"):
        if actor_values:
            st.markdown(f"**Actors:** {', '.join(actor_values[:10])}")

    metadata: Dict[str, str] = {
        "Runtime": runtime,
        "Rated": omdb_detail.get("Rated", "") if omdb_detail else "",
        "Writer": omdb_detail.get("Writer", "") if omdb_detail else "",
        "Awards": omdb_detail.get("Awards", "") if omdb_detail else "",
        "Box Office": omdb_detail.get("BoxOffice", "") if omdb_detail else "",
    }

    for label, value in metadata.items():
        if value and value != "N/A":
            st.markdown(f"**{label}:** {value}")

    synopsis = ""
    if omdb_detail and omdb_detail.get("Plot") and omdb_detail["Plot"] != "N/A":
        synopsis = omdb_detail["Plot"]
    elif movie.get("overview"):
        synopsis = movie["overview"]

    if synopsis:
        st.markdown("---")
        st.subheader("Synopsis")
        st.write(synopsis)


def ensure_session_defaults() -> None:
    """Initialise persistent selection lists in session state."""

    for key in ("selected_genres", "selected_directors", "selected_actors"):
        if key not in st.session_state:
            st.session_state[key] = []
    st.session_state.setdefault("_chips_caption_shown", False)
    st.session_state.setdefault("current_movie_id", None)


def make_checkbox_key(prefix: str, name: str) -> str:
    safe = "".join(ch if ch.isalnum() else "_" for ch in name.lower()).strip("_")
    if not safe:
        safe = "value"
    return f"{prefix}_{safe}"


def remove_filter_value(session_key: str, value: str) -> None:
    """Remove a value from a session-based filter list and refresh the app."""

    selections = st.session_state.get(session_key, [])
    if value not in selections:
        return
    st.session_state[session_key] = [item for item in selections if item != value]
    st.session_state["current_movie_id"] = None
    trigger_rerun()


def clear_all_filters() -> None:
    """Reset every active filter and start again from a random movie."""

    for key in ("selected_genres", "selected_directors", "selected_actors"):
        st.session_state[key] = []
    st.session_state["current_movie_id"] = None
    trigger_rerun()


def render_filter_badges(label: str, session_key: str, column) -> None:
    """Render removable buttons for the active filters within a column."""

    with column:
        st.caption(label)
        values = st.session_state.get(session_key, [])
        if not values:
            st.caption("None selected")
            return
        button_columns = st.columns(min(len(values), 3))
        for index, value in enumerate(values):
            button_column = button_columns[index % len(button_columns)]
            with button_column:
                key = make_checkbox_key(f"active_{session_key}", value)
                if st.button(f"❌ {value}", key=key, use_container_width=True):
                    remove_filter_value(session_key, value)


def render_active_filters_section() -> None:
    """Show the currently active filters with quick ways to remove them."""

    st.markdown("### Your Trail")
    selections = (
        st.session_state.get("selected_genres", []),
        st.session_state.get("selected_directors", []),
        st.session_state.get("selected_actors", []),
    )
    if not any(selections):
        st.caption("Start exploring by tapping a genre, director, or actor below.")
        return

    columns = st.columns(3)
    render_filter_badges("Genres", "selected_genres", columns[0])
    render_filter_badges("Directors", "selected_directors", columns[1])
    render_filter_badges("Actors", "selected_actors", columns[2])


ensure_session_defaults()

render_active_filters_section()

selected_genres: List[str] = st.session_state["selected_genres"]
selected_directors: List[str] = st.session_state["selected_directors"]
selected_actors: List[str] = st.session_state["selected_actors"]

poster_column_available = movie_table_has_column("poster_path")
movies = fetch_movies_for_filters(
    tuple(selected_genres),
    tuple(selected_directors),
    tuple(selected_actors),
    limit=200,
    include_poster_path=poster_column_available,
)

used_random_fallback = False
if not movies and (selected_genres or selected_directors or selected_actors):
    movies = fetch_movies_for_filters(
        tuple(), tuple(), tuple(), limit=200, include_poster_path=poster_column_available
    )
    used_random_fallback = True

if not movies:
    st.info("No movies available yet. Try refreshing your catalogue.")
    st.stop()


def movie_score(movie: dict) -> Tuple[int, int, int, int, float]:
    director_matches = movie.get("director_matches", 0)
    actor_matches = movie.get("actor_matches", 0)
    genre_matches = movie.get("genre_matches", 0)
    total = director_matches * 3 + actor_matches * 2 + genre_matches
    return (
        total,
        director_matches,
        actor_matches,
        genre_matches,
        float(movie.get("vote_average") or 0.0),
    )


if (selected_genres or selected_directors or selected_actors) and not used_random_fallback:
    movies_sorted = sorted(movies, key=movie_score, reverse=True)
else:
    movies_sorted = list(movies)

movies_sorted = movies_sorted[:60]
movie_lookup = {movie["tmdb_id"]: movie for movie in movies}

current_movie_id = st.session_state.get("current_movie_id")
current_movie = movie_lookup.get(current_movie_id)

if not current_movie:
    if not movies_sorted:
        current_movie = movies[0]
    elif selected_genres or selected_directors or selected_actors:
        current_movie = movies_sorted[0]
    else:
        current_movie = random.choice(movies_sorted)
    st.session_state["current_movie_id"] = current_movie["tmdb_id"]
    current_movie_id = current_movie["tmdb_id"]

action_columns = st.columns([1, 1, 1])
with action_columns[0]:
    if st.button("🔀 Surprise me", key="surprise_me"):
        st.session_state["current_movie_id"] = random.choice(movies)["tmdb_id"]
        trigger_rerun()
with action_columns[1]:
    if st.button("🧹 Clear filters", key="clear_filters"):
        clear_all_filters()
with action_columns[2]:
    if used_random_fallback:
        st.caption("No exact matches yet — showing random discoveries.")
    else:
        st.caption("Tip: tap the chips in the details to branch out.")

if used_random_fallback and (selected_genres or selected_directors or selected_actors):
    st.info(
        "We couldn't find matches for your current trail, so here are some random "
        "discoveries instead."
    )

tmdb_detail = fetch_tmdb_movie_detail(current_movie.get("tmdb_id"))
combined_movie = dict(current_movie)
omdb_detail = None
if tmdb_detail:
    for key, value in tmdb_detail.items():
        if key not in combined_movie or not combined_movie[key]:
            combined_movie[key] = value
    imdb_id = tmdb_detail.get("imdb_id")
    if imdb_id:
        omdb_detail = fetch_omdb_movie_detail(imdb_id)

st.markdown("## Current Discovery")
render_movie_detail(combined_movie, omdb_detail)

recommended_movies = [
    movie for movie in movies_sorted if movie.get("tmdb_id") != current_movie_id
]

if recommended_movies:
    st.divider()
    st.subheader("More movies to explore")
    explore_columns = st.columns(3)
    for index, movie in enumerate(recommended_movies[:9]):
        column = explore_columns[index % len(explore_columns)]
        with column:
            title = movie.get("title", "Unknown Title")
            year_text = movie.get("release_year", "N/A") or "N/A"
            rating_value = movie.get("vote_average")
            rating_text = (
                f"{float(rating_value):.1f}"
                if isinstance(rating_value, (int, float)) and float(rating_value) > 0
                else "N/A"
            )
            button_label = f"{title} ({year_text}) — ⭐ {rating_text}"
            if st.button(
                button_label,
                key=f"suggest_{movie['tmdb_id']}",
                use_container_width=True,
            ):
                st.session_state["current_movie_id"] = movie["tmdb_id"]
                trigger_rerun()

            match_bits: List[str] = []
            if movie.get("director_matches"):
                match_bits.append(f"{movie['director_matches']} director match")
            if movie.get("actor_matches"):
                match_bits.append(f"{movie['actor_matches']} actor match")
            if movie.get("genre_matches"):
                match_bits.append(f"{movie['genre_matches']} genre match")
            if match_bits:
                st.caption(" • ".join(match_bits))
            elif movie.get("genres"):
                st.caption(", ".join(movie["genres"]))
else:
    st.caption("You're at the end of the trail for now — try a surprise pick above!")
