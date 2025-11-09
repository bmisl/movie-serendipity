# serendipity_v3.py
import os
import random
import statistics
from typing import List, Optional

import requests
import streamlit as st

BASE_URL = "https://www.omdbapi.com/"
TMDB_BASE_URL = "https://api.themoviedb.org/3/person/popular"


def get_secret(key: str) -> Optional[str]:
    """Fetch configuration values from Streamlit secrets or the environment."""

    if hasattr(st, "secrets") and key in st.secrets:
        return st.secrets[key]
    return os.getenv(key)


OMDB_API_KEY = get_secret("OMDB_API_KEY")
TMDB_API_KEY = get_secret("TMDB_API_KEY")

FALLBACK_PEOPLE = {
    "Acting": [
        "Tom Hanks",
        "Scarlett Johansson",
        "Leonardo DiCaprio",
        "Denzel Washington",
        "Natalie Portman",
        "Meryl Streep",
        "Viola Davis",
        "Ryan Gosling",
        "Emma Stone",
        "Brad Pitt",
        "Cate Blanchett",
        "Mahershala Ali",
        "Keanu Reeves",
        "Michael B. Jordan",
        "Daniel Kaluuya",
    ],
    "Directing": [
        "Christopher Nolan",
        "Steven Spielberg",
        "Ridley Scott",
        "Quentin Tarantino",
        "Greta Gerwig",
        "Ava DuVernay",
        "Patty Jenkins",
        "Ryan Coogler",
        "Bong Joon Ho",
        "Denis Villeneuve",
        "Barry Jenkins",
        "Jordan Peele",
        "Alfonso Cuarón",
        "Kathryn Bigelow",
        "Chloé Zhao",
    ],
}


def ensure_api_key(key: Optional[str], label: str) -> str:
    """Show a helpful error if a required API key is missing."""

    if not key:
        st.error(
            f"Missing {label}. Add it to Streamlit secrets or as an environment variable "
            f"named {label}."
        )
        st.stop()
    return key


def fetch_tmdb_people(department: str, count: int = 15) -> List[str]:
    """Retrieve a random selection of people from TMDB for a given department."""

    if not TMDB_API_KEY:
        return FALLBACK_PEOPLE[department][:count]

    collected: List[str] = []
    pages_seen = set()
    total_pages = 1

    try:
        while len(collected) < count and len(pages_seen) < 10:
            page = random.randint(1, total_pages) if pages_seen else random.randint(1, 20)
            if page in pages_seen:
                continue
            pages_seen.add(page)

            response = requests.get(
                TMDB_BASE_URL,
                params={"api_key": TMDB_API_KEY, "language": "en-US", "page": page},
                timeout=10,
            )
            response.raise_for_status()
            payload = response.json()
            total_pages = payload.get("total_pages", total_pages)

            for person in payload.get("results", []):
                if person.get("known_for_department") != department:
                    continue
                name = person.get("name")
                if name and name not in collected:
                    collected.append(name)
        if len(collected) >= count:
            return random.sample(collected, count)
        fallback = FALLBACK_PEOPLE[department]
        combined = collected + [name for name in fallback if name not in collected]
        return combined[:count]
    except requests.RequestException:
        return FALLBACK_PEOPLE[department][:count]


st.title("🎬 Serendipitous Movie Picker")

ensure_api_key(OMDB_API_KEY, "OMDB_API_KEY")


def search_movies(term: str) -> List[dict]:
    """Fetch up to ~20 movie results by search term."""

    try:
        response = requests.get(
            BASE_URL,
            params={"s": term, "apikey": OMDB_API_KEY},
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()
    except requests.RequestException:
        st.error("Unable to reach OMDb right now. Please try again later.")
        return []

    if data.get("Response") == "True":
        return data.get("Search", [])
    return []


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
    return detail


def render_movie_detail(detail: dict) -> None:
    """Display the selected movie information."""

    if detail.get("Poster") and detail["Poster"] != "N/A":
        st.image(detail["Poster"], width=200)
    st.markdown(
        f"**{detail.get('Title', 'Unknown Title')} ({detail.get('Year', 'N/A')})** "
        f"⭐ {detail.get('imdbRating', 'N/A')}"
    )
    if detail.get("Genre"):
        st.caption(detail["Genre"])
    if detail.get("Plot") and detail["Plot"] != "N/A":
        st.write(detail["Plot"])


def movies_by_era(movies: List[dict], median_year: Optional[int], era: str) -> List[dict]:
    """Filter movies according to the selected release window."""

    if not movies or median_year is None or era == "All releases":
        return movies
    if era.startswith("Older"):
        return [
            movie
            for movie in movies
            if movie.get("Year", "").isdigit()
            and int(movie["Year"]) <= median_year
        ]
    if era.startswith("Newer"):
        return [
            movie
            for movie in movies
            if movie.get("Year", "").isdigit()
            and int(movie["Year"]) > median_year
        ]
    return movies


if "current_pick" not in st.session_state:
    st.session_state["current_pick"] = None
    st.session_state["current_pick_detail"] = None
    st.session_state["current_pick_meta"] = None


col1, col2, col3 = st.columns(3, gap="small")

with col1:
    filter_type = st.radio("Type", ["Genre", "Actor", "Director"], key="filter_type")

if filter_type == "Genre":
    choices = [
        "Action",
        "Adventure",
        "Comedy",
        "Drama",
        "Horror",
        "Sci-Fi",
        "Romance",
        "Thriller",
    ]
elif filter_type == "Actor":
    ensure_api_key(TMDB_API_KEY, "TMDB_API_KEY")
    choices = fetch_tmdb_people("Acting")
else:
    ensure_api_key(TMDB_API_KEY, "TMDB_API_KEY")
    choices = fetch_tmdb_people("Directing")

with col2:
    selection = st.radio(
        f"Select a {filter_type.lower()}",
        choices,
        key=f"selection_{filter_type.lower()}",
    )

movies = search_movies(selection)

years = []
for movie in movies:
    year = movie.get("Year", "")
    if year.isdigit():
        years.append(int(year))

median_year = int(statistics.median(years)) if years else None

with col3:
    if median_year is None:
        era = "All releases"
        st.markdown("**Release window**")
        st.caption("More data is needed for year-based picks.")
    else:
        era_options = [
            "All releases",
            f"Older (≤ {median_year})",
            f"Newer (> {median_year})",
        ]
        era = st.radio("Release window", era_options, key="release_window")
    reroll = st.button("🎲 Surprise me", key="reroll")

if median_year is not None:
    st.caption(f"📅 Median release year for {selection}: {median_year}")

filtered_movies = movies_by_era(movies, median_year, era)

meta = (filter_type, selection, era)
if st.session_state.get("current_pick_meta") != meta:
    st.session_state["current_pick_meta"] = meta
    st.session_state["current_pick"] = None
    st.session_state["current_pick_detail"] = None

if reroll:
    st.session_state["current_pick"] = None
    st.session_state["current_pick_detail"] = None

if not filtered_movies:
    st.warning("No movies available for that combination right now.")
else:
    if st.session_state.get("current_pick") is None:
        pick = random.choice(filtered_movies)
        detail = fetch_movie_detail(pick["imdbID"])
        if detail:
            st.session_state["current_pick"] = pick
            st.session_state["current_pick_detail"] = detail
    detail = st.session_state.get("current_pick_detail")
    if detail:
        render_movie_detail(detail)
