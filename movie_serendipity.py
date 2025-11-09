# serendipity_v3.py
import os
from typing import Dict, Iterable, List, Optional, Set, Tuple

import requests
import streamlit as st

BASE_URL = "https://www.omdbapi.com/"

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

SEARCH_SEEDS = [
    "the",
    "love",
    "night",
    "day",
    "man",
    "girl",
    "life",
    "dark",
    "world",
    "star",
    "war",
    "city",
    "last",
    "first",
    "king",
    "queen",
    "story",
    "blue",
    "red",
    "dream",
]

GENRES = [
    "Action",
    "Adventure",
    "Comedy",
    "Drama",
    "Horror",
    "Sci-Fi",
    "Romance",
    "Thriller",
]

DIRECTOR_COUNT = 9
ACTOR_COUNT = 20


def get_secret(key: str) -> Optional[str]:
    """Fetch configuration values from Streamlit secrets or the environment."""

    if hasattr(st, "secrets") and key in st.secrets:
        return st.secrets[key]
    return os.getenv(key)


OMDB_API_KEY = get_secret("OMDB_API_KEY")


def ensure_api_key(key: Optional[str], label: str) -> str:
    """Show a helpful error if a required API key is missing."""

    if not key:
        st.error(
            f"Missing {label}. Add it to Streamlit secrets or as an environment variable "
            f"named {label}."
        )
        st.stop()
    return key


st.title("🎬 Serendipitous Movie Picker")

ensure_api_key(OMDB_API_KEY, "OMDB_API_KEY")


@st.cache_data(show_spinner=False)
def search_movies(term: str, max_pages: int = 3) -> List[dict]:
    """Fetch a batch of movie search results from OMDb."""

    collected: List[dict] = []
    try:
        for page in range(1, max_pages + 1):
            response = requests.get(
                BASE_URL,
                params={
                    "s": term,
                    "apikey": OMDB_API_KEY,
                    "type": "movie",
                    "page": page,
                },
                timeout=10,
            )
            response.raise_for_status()
            data = response.json()
            if data.get("Response") != "True":
                break
            collected.extend(data.get("Search", []))
            total_results_text = data.get("totalResults", "")
            total_results = int(total_results_text) if total_results_text.isdigit() else 0
            if total_results and len(collected) >= total_results:
                break
    except requests.RequestException:
        st.error("Unable to reach OMDb right now. Please try again later.")
        return []

    return [movie for movie in collected if movie.get("Type", "").lower() == "movie"]


@st.cache_data(show_spinner=False)
def fetch_movie_detail(imdb_id: str) -> Optional[dict]:
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


def render_movie_detail(detail: dict) -> None:
    """Display the selected movie information."""

    if detail.get("Poster") and detail["Poster"] != "N/A":
        st.image(detail["Poster"], width=220)

    title = detail.get("Title", "Unknown Title")
    year = detail.get("Year", "N/A")
    rating = detail.get("imdbRating", "N/A")
    st.markdown(f"**{title} ({year})** ⭐ {rating}")

    metadata: Dict[str, str] = {
        "Genre": detail.get("Genre", ""),
        "Runtime": detail.get("Runtime", ""),
        "Rated": detail.get("Rated", ""),
        "Director": detail.get("Director", ""),
        "Writer": detail.get("Writer", ""),
        "Actors": detail.get("Actors", ""),
        "Awards": detail.get("Awards", ""),
        "Box Office": detail.get("BoxOffice", ""),
    }

    for label, value in metadata.items():
        if value and value != "N/A":
            st.markdown(f"**{label}:** {value}")

    if detail.get("Plot") and detail["Plot"] != "N/A":
        st.markdown("---")
        st.subheader("Synopsis")
        st.write(detail["Plot"])


def reset_director_and_actor() -> None:
    st.session_state.pop("director_choice", None)
    st.session_state.pop("actor_selection", None)


def reset_actor_selection() -> None:
    st.session_state.pop("actor_selection", None)


def split_people(value: Optional[str]) -> List[str]:
    if not value:
        return []
    return [person.strip() for person in value.split(",") if person.strip() and person.strip() != "N/A"]


@st.cache_data(show_spinner=False)
def collect_movies_for_genre(genre: str, max_movies: int = 120) -> List[dict]:
    """Gather a pool of movies matching the requested genre."""

    collected: Dict[str, dict] = {}
    lowered = genre.lower()

    for seed in SEARCH_SEEDS:
        summaries = search_movies(seed, max_pages=5)
        for summary in summaries:
            imdb_id = summary.get("imdbID")
            if not imdb_id or imdb_id in collected:
                continue
            detail = fetch_movie_detail(imdb_id)
            if not detail:
                continue
            genres_text = detail.get("Genre", "").lower()
            if lowered not in genres_text:
                continue
            if "documentary" in genres_text:
                continue
            collected[imdb_id] = detail
            if len(collected) >= max_movies:
                return list(collected.values())
    return list(collected.values())


def extract_directors(movies: Iterable[dict]) -> List[str]:
    unique: Set[str] = set()
    for movie in movies:
        unique.update(split_people(movie.get("Director")))
    return sorted(unique)


def extract_actors(movies: Iterable[dict]) -> List[str]:
    unique: Set[str] = set()
    for movie in movies:
        unique.update(split_people(movie.get("Actors")))
    return sorted(unique)


def filter_by_director(movies: Iterable[dict], director: str) -> List[dict]:
    if not director:
        return list(movies)
    director_lower = director.lower()
    return [movie for movie in movies if director_lower in movie.get("Director", "").lower()]


def movies_matching_actors(movies: Iterable[dict], actors: Iterable[str]) -> List[dict]:
    required = {actor.lower() for actor in actors}
    if not required:
        return list(movies)
    matching: List[dict] = []
    for movie in movies:
        actor_names = {actor.lower() for actor in split_people(movie.get("Actors"))}
        if required.issubset(actor_names):
            matching.append(movie)
    return matching


def rating_sort_key(movie: dict) -> Tuple[int, str]:
    raw_rating = movie.get("imdbRating", "0")
    try:
        rating_value = float(raw_rating)
    except (TypeError, ValueError):
        rating_value = 0.0
    return (int(rating_value * 10), movie.get("Title", ""))


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
    movies_for_genre = collect_movies_for_genre(genre_choice)

with col_director:
    st.subheader("Directors")
    if not genre_choice:
        st.info("Start by picking a genre.")
        director_choice: Optional[str] = None
    elif not movies_for_genre:
        st.warning("We couldn't find directors for that genre yet.")
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
            valid_movies = movies_matching_actors(movies_for_director, selected_actors)

if director_choice:
    matching_movies = movies_matching_actors(movies_for_director, st.session_state.get("actor_selection", []))
else:
    matching_movies = []

matching_movies = sorted(matching_movies, key=rating_sort_key, reverse=True)[:10]

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
    for index, detail in enumerate(matching_movies, start=1):
        title = detail.get("Title", "Unknown Title")
        year_text = detail.get("Year", "N/A")
        rating = detail.get("imdbRating", "N/A")
        label = f"{index}. {title} ({year_text}) — ⭐ {rating}"
        options.append((label, detail))

    labels = [label for label, _ in options]
    if st.session_state.get("match_choice") not in labels:
        st.session_state["match_choice"] = labels[0]

    selected_label = st.radio(
        "Select a movie to see the details",
        labels,
        key="match_choice",
    )

    selected_detail = next(detail for label, detail in options if label == selected_label)
    render_movie_detail(selected_detail)
