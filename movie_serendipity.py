# serendipity_v3.py
import os
import random
import sqlite3
from collections import defaultdict
from typing import Callable, Dict, List, Optional, Sequence, Tuple

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


st.set_page_config(layout="wide", initial_sidebar_state="collapsed")
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
                (
                    "EXISTS ("
                    " SELECT 1"
                    " FROM movie_people md"
                    " JOIN people pd ON pd.id = md.person_id"
                    " WHERE md.movie_id = m.id AND md.role = 'Director' AND pd.name IN ("
                    f"{placeholders}"
                    ")"
                    ")"
                )
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


def trigger_rerun() -> None:
    """Request a Streamlit rerun using the supported API for the current version."""

    if hasattr(st, "rerun"):
        st.rerun()
    elif hasattr(st, "experimental_rerun"):
        st.experimental_rerun()


def pick_random_value(values: Sequence[str]) -> Optional[str]:
    """Return a random non-empty value from the provided sequence."""

    valid = [value for value in values if value]
    if not valid:
        return None
    return random.choice(valid)


def ensure_filter_defaults() -> None:
    """Initialise the single-value filters with random defaults."""

    if "filter_genre" not in st.session_state:
        st.session_state["filter_genre"] = pick_random_value(load_available_genres())
    if "filter_director" not in st.session_state:
        st.session_state["filter_director"] = pick_random_value(
            load_directors_for_genres(tuple())
        )
    if "filter_actor" not in st.session_state:
        st.session_state["filter_actor"] = pick_random_value(
            load_actors_for_filters(tuple(), tuple())
        )
    st.session_state.setdefault("current_movie_id", None)


def randomise_filters() -> None:
    """Replace every active filter with a fresh random value."""

    st.session_state["filter_genre"] = pick_random_value(load_available_genres())
    st.session_state["filter_director"] = pick_random_value(
        load_directors_for_genres(tuple())
    )
    st.session_state["filter_actor"] = pick_random_value(
        load_actors_for_filters(tuple(), tuple())
    )
    st.session_state["current_movie_id"] = None
    trigger_rerun()


def apply_filter_change(session_key: str, value: Optional[str]) -> None:
    """Persist a new filter value and refresh the recommendations."""

    if st.session_state.get(session_key) == value:
        return
    st.session_state[session_key] = value
    st.session_state["current_movie_id"] = None
    trigger_rerun()


def use_value_from_movie(session_key: str, values: Sequence[str]) -> None:
    """Pull the first available value from the current movie into a filter."""

    first_value = next((item for item in values if item), None)
    if not first_value:
        st.warning("The current movie does not have a value for this filter yet.")
        return
    apply_filter_change(session_key, first_value)


def parse_table_selection(widget_state: Optional[dict]) -> Tuple[Optional[int], Optional[str]]:
    """Extract the first selected row index and column key from a dataframe widget."""

    if not isinstance(widget_state, dict):
        return None, None

    selection = widget_state.get("selection")
    row_index: Optional[int] = None
    column_key: Optional[str] = None

    def coerce_index(value: object) -> Optional[int]:
        if isinstance(value, list):
            for item in value:
                coerced = coerce_index(item)
                if coerced is not None:
                    return coerced
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    if isinstance(selection, dict):
        row_index = coerce_index(selection.get("rows")) or row_index
        columns = selection.get("columns") or selection.get("cols")
        if isinstance(columns, list) and columns:
            column_key = str(columns[0])
        elif isinstance(columns, str):
            column_key = columns

        active_cell = selection.get("active_cell") or selection.get("activeCell")
        if isinstance(active_cell, dict):
            row_index = coerce_index(active_cell.get("row")) or row_index
            active_column = active_cell.get("column") or active_cell.get("col")
            if active_column:
                column_key = str(active_column)

        cells = selection.get("cells") or selection.get("cell")
        if isinstance(cells, list) and cells:
            first_cell = cells[0]
            if isinstance(first_cell, dict):
                row_index = coerce_index(first_cell.get("row")) or row_index
                cell_column = first_cell.get("column") or first_cell.get("col")
                if cell_column:
                    column_key = str(cell_column)

    if row_index is None:
        selected_rows = widget_state.get("selected_rows")
        if isinstance(selected_rows, list) and selected_rows:
            first_entry = selected_rows[0]
            if isinstance(first_entry, dict):
                row_index = coerce_index(first_entry.get("index")) or coerce_index(
                    first_entry.get("row")
                )
            else:
                row_index = coerce_index(first_entry)

    return row_index, column_key


def normalise_cell_value_for_filter(column: Optional[str], value: object) -> Optional[Tuple[str, str]]:
    """Translate a table selection into a sidebar filter assignment."""

    if not column or not isinstance(value, str):
        return None

    parts = [
        part.strip()
        for chunk in value.replace("\n", ",").split(",")
        for part in [chunk]
        if part.strip()
    ]
    if not parts:
        return None

    primary = parts[0]

    if column == "Genres":
        normalised = DB_GENRE_TO_UI.get(primary, primary)
        return "filter_genre", normalised
    if column == "Director":
        return "filter_director", primary
    if column == "Actors":
        return "filter_actor", primary

    return None


def gather_movie_metadata(
    movie: dict, omdb_detail: Optional[dict]
) -> Tuple[List[str], List[str], List[str]]:
    """Return combined genre, director, and actor lists for the movie."""

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
    return genre_values, director_values, actor_values


def render_movie_detail(
    movie: dict,
    omdb_detail: Optional[dict],
    *,
    on_surprise: Optional[Callable[[], None]] = None,
) -> Tuple[List[str], List[str], List[str]]:
    """Display details for the selected movie using OMDb data with TMDB fallbacks."""

    poster_url: Optional[str] = None
    if omdb_detail and omdb_detail.get("Poster") and omdb_detail["Poster"] != "N/A":
        poster_url = omdb_detail["Poster"]
    elif movie.get("poster_url"):
        poster_url = movie["poster_url"]

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

    rated_value = omdb_detail.get("Rated", "") if omdb_detail else ""
    writer_value = omdb_detail.get("Writer", "") if omdb_detail else ""
    awards_value = omdb_detail.get("Awards", "") if omdb_detail else ""

    synopsis = ""
    if omdb_detail and omdb_detail.get("Plot") and omdb_detail["Plot"] != "N/A":
        synopsis = omdb_detail["Plot"]
    elif movie.get("overview"):
        synopsis = movie["overview"]

    genre_values, director_values, actor_values = gather_movie_metadata(movie, omdb_detail)

    detail_container = st.container()
    with detail_container:
        layout_columns = st.columns([1, 2])
        with layout_columns[0]:
            if poster_url:
                st.image(poster_url, width=260)
        with layout_columns[1]:
            if on_surprise:
                detail_sections = st.columns([6, 1])
                detail_body = detail_sections[0]
                with detail_sections[1]:
                    if st.button("🔀 Surprise me", key="surprise_me", use_container_width=True):
                        on_surprise()
            else:
                detail_body = st.container()

            with detail_body:
                st.markdown(f"### {title} ({year})")
                st.markdown(f"**⭐ Rating:** {rating_value}")
                if genre_values:
                    st.markdown(f"**Genres:** {', '.join(genre_values)}")
                if director_values:
                    st.markdown(f"**Director:** {', '.join(director_values)}")
                if actor_values:
                    st.markdown(f"**Actors:** {', '.join(actor_values[:10])}")
                if rated_value and rated_value != "N/A":
                    st.markdown(f"**Rated:** {rated_value}")
                if writer_value and writer_value != "N/A":
                    st.markdown(f"**Writer:** {writer_value}")
                if awards_value and awards_value != "N/A":
                    st.markdown(f"**Awards:** {awards_value}")


        if synopsis:
            st.markdown("**Synopsis**")
            st.write(synopsis)

    return genre_values, director_values, actor_values


def render_filter_sidebar(
    genre_options: Sequence[str],
    director_options: Sequence[str],
    actor_options: Sequence[str],
    current_movie_genres: Sequence[str],
    current_movie_directors: Sequence[str],
    current_movie_actors: Sequence[str],
    table_selection: Optional[dict],
) -> None:
    """Render the slide-out sidebar controls for refining the movie list."""

    with st.sidebar:
        st.header("Discovery filters")
        st.caption("Pick a genre, director, or actor to reshape your matches.")

        if st.button("🎲 Randomise filters", key="randomise_filters_sidebar"):
            randomise_filters()

        selection_details: Optional[Tuple[str, str]] = None
        selected_column = None
        selected_value = None
        if isinstance(table_selection, dict):
            selected_column = table_selection.get("column")
            selected_value = table_selection.get("value")
            selection_details = normalise_cell_value_for_filter(selected_column, selected_value)

        if selected_column and selected_value:
            st.caption(f"Selected cell → {selected_column}: {selected_value}")
        else:
            st.caption("Select a table cell to reuse it as a filter.")

        if st.button("Make selection a filter", key="apply_table_selection"):
            if selection_details:
                session_key, selection_value = selection_details
                if session_key == "filter_genre" and selection_value not in genre_options:
                    st.info("That genre is not available right now.")
                elif session_key == "filter_director" and selection_value not in director_options:
                    st.info("That director is not available right now.")
                elif session_key == "filter_actor" and selection_value not in actor_options:
                    st.info("That actor is not available right now.")
                else:
                    apply_filter_change(session_key, selection_value)
            elif selected_column:
                st.info("Choose a genre, director, or actor cell to apply it as a filter.")

        st.divider()

        current_genre = st.session_state.get("filter_genre")
        genre_display = ["Any", *genre_options]
        genre_label = current_genre if current_genre else "Any"
        if genre_label not in genre_display:
            genre_label = "Any"
        genre_choice = st.selectbox(
            "Genre",
            genre_display,
            index=genre_display.index(genre_label),
        )
        apply_filter_change("filter_genre", None if genre_choice == "Any" else genre_choice)

        if st.button(
            "Use current movie genre",
            key="use_current_genre",
            disabled=not current_movie_genres,
        ):
            use_value_from_movie("filter_genre", current_movie_genres)

        st.divider()

        current_director = st.session_state.get("filter_director")
        director_display = ["Any", *director_options]
        director_label = current_director if current_director else "Any"
        if director_label not in director_display:
            director_label = "Any"
        director_choice = st.selectbox(
            "Director",
            director_display,
            index=director_display.index(director_label),
        )
        apply_filter_change(
            "filter_director", None if director_choice == "Any" else director_choice
        )

        if st.button(
            "Use current movie director",
            key="use_current_director",
            disabled=not current_movie_directors,
        ):
            use_value_from_movie("filter_director", current_movie_directors)

        st.divider()

        current_actor = st.session_state.get("filter_actor")
        actor_display = ["Any", *actor_options]
        actor_label = current_actor if current_actor else "Any"
        if actor_label not in actor_display:
            actor_label = "Any"
        actor_choice = st.selectbox(
            "Actor",
            actor_display,
            index=actor_display.index(actor_label),
        )
        apply_filter_change("filter_actor", None if actor_choice == "Any" else actor_choice)

        if st.button(
            "Use current movie actor",
            key="use_current_actor",
            disabled=not current_movie_actors,
        ):
            use_value_from_movie("filter_actor", current_movie_actors)


def render_recommendation_table(
    current_movie: dict,
    movies: Sequence[dict],
    *,
    table_height: int = 420,
) -> Optional[int]:
    """Render a scrollable table of movie matches and return the chosen movie id."""

    table_movies: List[dict] = []
    seen_ids: set[int] = set()

    if current_movie:
        current_id = current_movie.get("tmdb_id")
        if current_id is not None:
            table_movies.append(current_movie)
            seen_ids.add(current_id)

    for movie in movies:
        movie_id = movie.get("tmdb_id")
        if movie_id is None or movie_id in seen_ids:
            continue
        table_movies.append(movie)
        seen_ids.add(movie_id)

    if not table_movies:
        return None

    table_rows: List[Dict[str, str]] = []
    option_ids: List[int] = []

    for movie in table_movies:
        movie_id = movie.get("tmdb_id")
        if movie_id is None:
            continue

        option_ids.append(movie_id)
        title = movie.get("title", "Unknown Title")
        year_text = movie.get("release_year") or ""
        rating_value = movie.get("vote_average")
        rating_text = (
            f"{float(rating_value):.1f}"
            if isinstance(rating_value, (int, float)) and float(rating_value) > 0
            else ""
        )

        match_bits: List[str] = []
        if movie.get("director_matches"):
            match_bits.append(f"{movie['director_matches']} director")
        if movie.get("actor_matches"):
            match_bits.append(f"{movie['actor_matches']} actor")
        if movie.get("genre_matches"):
            match_bits.append(f"{movie['genre_matches']} genre")

        row = {
            "Status": "Current"
            if current_movie and movie_id == current_movie.get("tmdb_id")
            else "",
            "Title": title,
            "Year": year_text,
            "Rating": rating_text,
            "Genres": ", ".join(movie.get("genres", [])),
            "Director": ", ".join(movie.get("directors", [])[:2]),
            "Actors": ", ".join(movie.get("actors", [])[:3]),
            "Matches": " / ".join(match_bits),
        }
        table_rows.append(row)

    column_order = [
        "Status",
        "Title",
        "Year",
        "Rating",
        "Genres",
        "Director",
        "Actors",
        "Matches",
    ]

    table_key = "movie_recommendations_table"
    st.dataframe(
        table_rows,
        use_container_width=True,
        height=table_height,
        hide_index=True,
        column_order=column_order,
        key=table_key,
        on_select="rerun",
    )

    if not option_ids:
        st.session_state.pop("table_selection_info", None)
        return None

    widget_state = st.session_state.get(table_key)
    row_number, column_key = parse_table_selection(widget_state)

    if row_number is None or row_number < 0 or row_number >= len(option_ids):
        st.session_state.pop("table_selection_info", None)
        return None

    selected_row = table_rows[row_number]
    selected_value = selected_row.get(column_key) if column_key else None

    st.session_state["table_selection_info"] = {
        "row": row_number,
        "column": column_key,
        "value": selected_value,
        "movie_id": option_ids[row_number],
    }

    return option_ids[row_number]




ensure_filter_defaults()

selected_genres: List[str] = []
selected_directors: List[str] = []
selected_actors: List[str] = []

genre_filter = st.session_state.get("filter_genre")
director_filter = st.session_state.get("filter_director")
actor_filter = st.session_state.get("filter_actor")

if genre_filter:
    selected_genres = [genre_filter]
if director_filter:
    selected_directors = [director_filter]
if actor_filter:
    selected_actors = [actor_filter]

genre_options = load_available_genres()
director_options = load_directors_for_genres(tuple(selected_genres))
if director_filter and director_filter not in director_options:
    st.session_state["filter_director"] = None
    director_filter = None
    selected_directors = []
    director_options = load_directors_for_genres(tuple(selected_genres))

actor_options = load_actors_for_filters(tuple(selected_genres), tuple(selected_directors))
if actor_filter and actor_filter not in actor_options:
    st.session_state["filter_actor"] = None
    actor_filter = None
    selected_actors = []
    actor_options = load_actors_for_filters(tuple(selected_genres), tuple(selected_directors))

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

def handle_surprise() -> None:
    source = movies_sorted if movies_sorted else movies
    st.session_state["current_movie_id"] = random.choice(source)["tmdb_id"]
    trigger_rerun()

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

current_movie_genres, current_movie_directors, current_movie_actors = render_movie_detail(
    combined_movie,
    omdb_detail,
    on_surprise=handle_surprise,
)

render_filter_sidebar(
    genre_options,
    director_options,
    actor_options,
    current_movie_genres,
    current_movie_directors,
    current_movie_actors,
    st.session_state.get("table_selection_info"),
)

if used_random_fallback and (selected_genres or selected_directors or selected_actors):
    st.info(
        "We couldn't find matches for your current trail, so here are some random "
        "discoveries instead."
    )

st.divider()
selected_from_table = render_recommendation_table(current_movie, movies_sorted)

if selected_from_table and selected_from_table != current_movie_id:
    st.session_state["current_movie_id"] = selected_from_table
    trigger_rerun()

if len(movies_sorted) <= 1:
    st.caption("You're at the end of the trail for now — try a surprise pick above!")
