# serendipity_v3.py
import os
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

import requests
import streamlit as st

BASE_URL = "https://www.omdbapi.com/"
TMDB_BASE_URL = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w500"

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
def discover_movies_for_genre(genre: str, max_pages: int = 5, max_movies: int = 120) -> List[dict]:
    """Collect a TMDB-backed pool of movies for the selected genre."""

    genre_id = TMDB_GENRE_IDS.get(genre)
    if not genre_id:
        return []

    collected: List[dict] = []
    seen_ids: Set[str] = set()

    for page in range(1, max_pages + 1):
        payload = tmdb_get(
            "discover/movie",
            params={
                "with_genres": genre_id,
                "page": page,
                "include_adult": "false",
                "sort_by": "popularity.desc",
            },
        )
        if not payload:
            break

        for item in payload.get("results", []):
            movie_id = item.get("id")
            if not movie_id:
                continue

            detail = fetch_tmdb_movie_detail(int(movie_id))
            if not detail:
                continue

            imdb_id = detail.get("imdb_id")
            if not imdb_id or imdb_id in seen_ids:
                continue

            if not detail.get("directors") or not detail.get("actors"):
                continue

            seen_ids.add(imdb_id)
            collected.append(detail)
            if len(collected) >= max_movies:
                return collected

        total_pages = payload.get("total_pages")
        if isinstance(total_pages, int) and page >= total_pages:
            break

    return collected


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


def reset_director_and_actor() -> None:
    st.session_state.pop("director_choice", None)
    st.session_state.pop("actor_selection", None)


def reset_actor_selection() -> None:
    st.session_state.pop("actor_selection", None)


def extract_directors(movies: Iterable[dict]) -> List[str]:
    unique: Set[str] = set()
    for movie in movies:
        unique.update(movie.get("directors", []))
    return sorted(unique)


def extract_actors(movies: Iterable[dict]) -> List[str]:
    unique: Set[str] = set()
    for movie in movies:
        unique.update(movie.get("actors", []))
    return sorted(unique)


def filter_by_director(movies: Iterable[dict], director: str) -> List[dict]:
    if not director:
        return list(movies)
    return [movie for movie in movies if director in movie.get("directors", [])]


def movies_matching_actors(movies: Iterable[dict], actors: Sequence[str]) -> List[dict]:
    required = {actor for actor in actors if actor}
    if not required:
        return list(movies)
    matching: List[dict] = []
    for movie in movies:
        actor_names = set(movie.get("actors", []))
        if required.issubset(actor_names):
            matching.append(movie)
    return matching


def rating_sort_key(movie: dict) -> Tuple[int, str]:
    rating_value = float(movie.get("vote_average") or 0.0)
    return (int(rating_value * 10), movie.get("title", ""))


col_genre, col_director, col_actor = st.columns(3, gap="large")

with col_genre:
    st.subheader("Genres")
    genre_choice = st.radio(
        "Pick a genre",
        GENRES,
        index=None,
        key="genre_choice",
        on_change=reset_director_and_actor,
    )

movies_for_genre: List[dict] = []
if genre_choice:
    movies_for_genre = discover_movies_for_genre(genre_choice)

with col_director:
    st.subheader("Directors")
    if not genre_choice:
        st.info("Start by picking a genre.")
        director_choice: Optional[str] = None
    elif not movies_for_genre:
        st.warning(
            "We couldn't load any movies for that genre yet. Please verify your TMDB "
            "API key or try a different genre."
        )
        director_choice = None
    else:
        directors = extract_directors(movies_for_genre)
        if not directors:
            st.warning("No directors available for the selected genre.")
            director_choice = None
        else:
            if (
                "director_choice" in st.session_state
                and st.session_state["director_choice"] not in directors
            ):
                st.session_state.pop("director_choice", None)
            director_choice = st.radio(
                "Pick a director",
                directors,
                index=None,
                key="director_choice",
                on_change=reset_actor_selection,
            )

movies_for_director: List[dict] = []
if director_choice:
    movies_for_director = filter_by_director(movies_for_genre, director_choice)

with col_actor:
    st.subheader("Actors")
    if not director_choice:
        st.info("Select a director to load actors.")
        selected_actors: List[str] = []
    else:
        current_selection: List[str] = st.session_state.get("actor_selection", [])
        valid_movies = movies_matching_actors(movies_for_director, current_selection)
        if not valid_movies:
            current_selection = []
            st.session_state["actor_selection"] = []
            valid_movies = movies_for_director
        available_actors = extract_actors(valid_movies)
        if not available_actors:
            st.warning("No actors available for the current filters.")
            selected_actors = []
        else:
            selected_actors = st.multiselect(
                "Pick one or more actors",
                available_actors,
                key="actor_selection",
                help="Only actors who appear in at least one matching movie are listed.",
            )

if director_choice:
    matching_movies = movies_matching_actors(
        movies_for_director,
        st.session_state.get("actor_selection", []),
    )
else:
    matching_movies = []

matching_movies = sorted(matching_movies, key=rating_sort_key, reverse=True)

st.divider()
st.subheader("Matched Movies")

if not genre_choice or not director_choice:
    st.session_state.pop("match_choice", None)

if not genre_choice:
    st.info("Pick a genre to begin exploring movies.")
elif not director_choice:
    st.info("Choose a director to see available movies.")
elif not matching_movies:
    st.session_state.pop("match_choice", None)
    st.info("No movies available for the current combination. Try adjusting the actors.")
else:
    options = []
    for index, movie in enumerate(matching_movies[:10], start=1):
        omdb_detail = (
            fetch_omdb_movie_detail(movie.get("imdb_id")) if movie.get("imdb_id") else None
        )
        title = (
            omdb_detail.get("Title")
            if omdb_detail and omdb_detail.get("Title")
            else movie.get("title", "Unknown Title")
        )
        year_text = (
            omdb_detail.get("Year")
            if omdb_detail and omdb_detail.get("Year")
            else movie.get("release_year", "N/A")
        )
        if omdb_detail and omdb_detail.get("imdbRating") and omdb_detail["imdbRating"] != "N/A":
            rating_text = omdb_detail["imdbRating"]
        elif movie.get("vote_average"):
            rating_text = f"{movie['vote_average']:.1f}"
        else:
            rating_text = "N/A"
        label = f"{index}. {title} ({year_text}) — ⭐ {rating_text}"
        options.append((label, movie, omdb_detail))

    if not options:
        st.info("No movies available for the current combination. Try adjusting the actors.")
    else:
        labels = [label for label, _, _ in options]
        if st.session_state.get("match_choice") not in labels:
            st.session_state["match_choice"] = labels[0]

        selected_label = st.radio(
            "Select a movie to see the details",
            labels,
            key="match_choice",
        )

        selected_movie, selected_omdb = next(
            (movie, omdb) for label, movie, omdb in options if label == selected_label
        )
        render_movie_detail(selected_movie, selected_omdb)
