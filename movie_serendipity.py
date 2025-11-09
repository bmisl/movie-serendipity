# serendipity_v3.py
import os
import sqlite3
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


@st.cache_data(show_spinner=False)
def fetch_movies_for_filters(
    genres: Tuple[str, ...],
    directors: Tuple[str, ...],
    actors: Tuple[str, ...],
    limit: int = 200,
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

        if not db_genres and not director_names and not actor_names:
            cur.execute(
                """
                SELECT m.id, m.title, m.year, m.runtime, m.vote_average, m.overview, m.popularity
                FROM movies m
                ORDER BY RANDOM()
                LIMIT ?
                """,
                (limit,),
            )
            rows = cur.fetchall()
            movie_ids = [row[0] for row in rows]
            directors_map, actors_map = collect_people_for_movies(movie_ids)
            return [
                build_movie_payload(row, directors_map, actors_map, 0, 0, 0)
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
        return [
            build_movie_payload(row, directors_map, actors_map, row[7], row[8], row[9])
            for row in rows
        ]
    finally:
        conn.close()


def build_movie_payload(
    row: Tuple,
    directors_map: Dict[int, List[str]],
    actors_map: Dict[int, List[str]],
    genre_matches: int,
    director_matches: int,
    actor_matches: int,
) -> dict:
    """Construct a movie payload with standardised fields and scoring metadata."""

    movie_id, title, year, runtime, vote_average, overview, _popularity = row[:7]
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
        "poster_url": None,
        "runtime_text": runtime_text,
        "genre_matches": int(genre_matches or 0),
        "director_matches": int(director_matches or 0),
        "actor_matches": int(actor_matches or 0),
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

    director_text = ""
    if omdb_detail and omdb_detail.get("Director") and omdb_detail["Director"] != "N/A":
        director_text = omdb_detail["Director"]
    elif movie.get("directors"):
        director_text = ", ".join(movie["directors"])

    actors_text = ""
    if omdb_detail and omdb_detail.get("Actors") and omdb_detail["Actors"] != "N/A":
        actors_text = omdb_detail["Actors"]
    elif movie.get("actors"):
        actors_text = ", ".join(movie["actors"][:10])

    metadata: Dict[str, str] = {
        "Genre": omdb_detail.get("Genre", "") if omdb_detail else "",
        "Runtime": runtime,
        "Rated": omdb_detail.get("Rated", "") if omdb_detail else "",
        "Director": director_text,
        "Writer": omdb_detail.get("Writer", "") if omdb_detail else "",
        "Actors": actors_text,
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
    st.session_state.setdefault("director_search", "")
    st.session_state.setdefault("actor_search", "")


def make_checkbox_key(prefix: str, name: str) -> str:
    safe = "".join(ch if ch.isalnum() else "_" for ch in name.lower()).strip("_")
    if not safe:
        safe = "value"
    return f"{prefix}_{safe}"


def render_checkbox_group(
    options: Sequence[str],
    selected_key: str,
    prefix: str,
) -> None:
    """Render a column of checkboxes and sync the values to session state."""

    selections = set(st.session_state[selected_key])
    for option in options:
        option_key = make_checkbox_key(prefix, option)
        if option_key not in st.session_state:
            st.session_state[option_key] = option in selections
        checked = st.checkbox(option, key=option_key)
        if checked:
            selections.add(option)
        elif option in selections:
            selections.remove(option)
    st.session_state[selected_key] = sorted(selections)


def handle_summary_removals(
    summary_key_prefix: str,
    selections: List[str],
    checkbox_prefix: str,
) -> List[str]:
    """Display removable checkboxes for the summary bar and return remaining selections."""

    remaining: List[str] = []
    removals: List[str] = []
    for item in selections:
        summary_key = f"summary_{summary_key_prefix}_{make_checkbox_key(summary_key_prefix, item)}"
        if summary_key not in st.session_state:
            st.session_state[summary_key] = True
        keep = st.checkbox(item, key=summary_key)
        if keep:
            remaining.append(item)
        else:
            removals.append(item)

    for item in removals:
        column_key = make_checkbox_key(checkbox_prefix, item)
        st.session_state.pop(
            f"summary_{summary_key_prefix}_{make_checkbox_key(summary_key_prefix, item)}",
            None,
        )
        if column_key in st.session_state:
            st.session_state[column_key] = False

    if removals:
        return [item for item in selections if item not in removals]
    return remaining


ensure_session_defaults()

selected_genres: List[str] = st.session_state["selected_genres"]
selected_directors: List[str] = st.session_state["selected_directors"]
selected_actors: List[str] = st.session_state["selected_actors"]

summary_container = st.container()
with summary_container:
    st.markdown("### Selection Summary")
    if not (selected_genres or selected_directors or selected_actors):
        st.caption("Make selections below to refine your movie matches.")
    else:
        summary_cols = st.columns(3)
        with summary_cols[0]:
            st.caption("Genres")
            if selected_genres:
                updated_genres = handle_summary_removals("genre", selected_genres, "genre")
            else:
                st.caption("None selected")
                updated_genres = selected_genres
        with summary_cols[1]:
            st.caption("Directors")
            if selected_directors:
                updated_directors = handle_summary_removals("director", selected_directors, "director")
            else:
                st.caption("None selected")
                updated_directors = selected_directors
        with summary_cols[2]:
            st.caption("Actors")
            if selected_actors:
                updated_actors = handle_summary_removals("actor", selected_actors, "actor")
            else:
                st.caption("None selected")
                updated_actors = selected_actors

        st.session_state["selected_genres"] = updated_genres
        st.session_state["selected_directors"] = updated_directors
        st.session_state["selected_actors"] = updated_actors

available_genres = load_available_genres()

genre_col, director_col, actor_col = st.columns([1.1, 2.2, 2.2], gap="small")

with genre_col:
    st.subheader("Genres")
    render_checkbox_group(available_genres, "selected_genres", "genre")

selected_genres = st.session_state["selected_genres"]

with director_col:
    st.subheader("Directors")
    director_options = load_directors_for_genres(tuple(selected_genres))
    if director_options:
        query = st.text_input(
            "Search directors",
            placeholder="Start typing to filter…",
            key="director_search",
        ).strip().lower()
        if query:
            director_options = [
                name for name in director_options if query in name.lower()
            ]
        render_checkbox_group(director_options, "selected_directors", "director")
    else:
        st.caption("No directors yet for the current selection.")

selected_directors = st.session_state["selected_directors"]

with actor_col:
    st.subheader("Actors")
    actor_options = load_actors_for_filters(tuple(selected_genres), tuple(selected_directors))
    if actor_options:
        query = st.text_input(
            "Search actors",
            placeholder="Start typing to filter…",
            key="actor_search",
        ).strip().lower()
        if query:
            actor_options = [name for name in actor_options if query in name.lower()]
        render_checkbox_group(actor_options, "selected_actors", "actor")
    else:
        st.caption("No actors yet for the current selection.")

selected_actors = st.session_state["selected_actors"]

movies = fetch_movies_for_filters(
    tuple(selected_genres), tuple(selected_directors), tuple(selected_actors), limit=200
)

used_random_fallback = False
if not movies and (selected_genres or selected_directors or selected_actors):
    movies = fetch_movies_for_filters(tuple(), tuple(), tuple(), limit=200)
    used_random_fallback = True

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
    movies_sorted = sorted(movies, key=movie_score, reverse=True)[:20]
else:
    movies_sorted = movies[:20]

st.divider()
st.subheader("Matched Movies")

if not movies_sorted:
    st.info("No movies available yet. Try adjusting your filters or refresh the database.")
else:
    options = []
    for index, movie in enumerate(movies_sorted, start=1):
        title = movie.get("title", "Unknown Title")
        year_text = movie.get("release_year", "N/A") or "N/A"
        rating_value = movie.get("vote_average")
        rating_text = (
            f"{float(rating_value):.1f}"
            if isinstance(rating_value, (int, float)) and float(rating_value) > 0
            else "N/A"
        )
        match_bits: List[str] = []
        if movie.get("director_matches"):
            match_bits.append(f"{movie['director_matches']} director match")
        if movie.get("actor_matches"):
            match_bits.append(f"{movie['actor_matches']} actor match")
        if movie.get("genre_matches"):
            match_bits.append(f"{movie['genre_matches']} genre match")
        match_text = " • ".join(match_bits)
        label = f"{index}. {title} ({year_text}) — ⭐ {rating_text}"
        if match_text:
            label = f"{label} — {match_text}"
        options.append((label, movie))

    labels = [label for label, _ in options]
    default_label = labels[0] if labels else None
    selected_label = st.radio(
        "Select a movie to see the details",
        labels,
        index=0 if default_label else None,
        key="movie_choice",
    )

    selected_movie = next(movie for label, movie in options if label == selected_label)
    tmdb_detail = fetch_tmdb_movie_detail(selected_movie.get("tmdb_id"))
    combined_movie = dict(selected_movie)
    omdb_detail = None
    if tmdb_detail:
        for key, value in tmdb_detail.items():
            if key not in combined_movie or not combined_movie[key]:
                combined_movie[key] = value
        imdb_id = tmdb_detail.get("imdb_id")
        if imdb_id:
            omdb_detail = fetch_omdb_movie_detail(imdb_id)
    render_movie_detail(combined_movie, omdb_detail)
