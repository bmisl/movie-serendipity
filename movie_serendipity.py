# serendipity_v3.py
import os
import random
import re
from typing import Dict, List, Optional

import requests
import streamlit as st

BASE_URL = "https://www.omdbapi.com/"
TMDB_BASE_URL = "https://api.themoviedb.org/3/person/popular"

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


if "director_choices" not in st.session_state:
    directors = fetch_tmdb_people("Directing", count=20)
    random.shuffle(directors)
    st.session_state["director_choices"] = directors[:DIRECTOR_COUNT]

if "actor_choices" not in st.session_state:
    actors = fetch_tmdb_people("Acting", count=40)
    random.shuffle(actors)
    st.session_state["actor_choices"] = actors[:ACTOR_COUNT]

if "year_filter" not in st.session_state:
    st.session_state["year_filter"] = "all"

if "match_choice" not in st.session_state:
    st.session_state["match_choice"] = None

col1, col2, col3 = st.columns(3, gap="medium")

with col1:
    st.subheader("Genres")
    genre_choice = st.radio("Pick a genre", GENRES, key="genre_choice")

    st.divider()
    st.subheader("Directors")
    randomize_directors = st.button("🔀 Randomize directors")
    if randomize_directors:
        directors = fetch_tmdb_people("Directing", count=20)
        random.shuffle(directors)
        st.session_state["director_choices"] = directors[:DIRECTOR_COUNT]
        st.session_state["director_radio"] = st.session_state["director_choices"][0]

    if "director_radio" not in st.session_state or st.session_state["director_radio"] not in st.session_state["director_choices"]:
        st.session_state["director_radio"] = st.session_state["director_choices"][0]

    director_choice = st.radio(
        "Select a director",
        st.session_state["director_choices"],
        key="director_radio",
    )

with col2:
    st.subheader("Actors")
    randomize_actors = st.button("🔀 Randomize actors")
    if randomize_actors:
        actors = fetch_tmdb_people("Acting", count=40)
        random.shuffle(actors)
        st.session_state["actor_choices"] = actors[:ACTOR_COUNT]
        for idx in range(ACTOR_COUNT):
            st.session_state.pop(f"actor_cb_{idx}", None)

    actor_columns = st.columns(4)
    selected_actors: List[str] = []
    for idx, actor in enumerate(st.session_state["actor_choices"]):
        column = actor_columns[idx % len(actor_columns)]
        with column:
            if st.checkbox(actor, key=f"actor_cb_{idx}"):
                selected_actors.append(actor)

with col3:
    st.subheader("Year")
    st.number_input(
        "Center year",
        min_value=1900,
        max_value=2100,
        value=st.session_state.get("center_year", 2000),
        step=1,
        key="center_year",
    )

    older_clicked = st.button("Older (≤ center)")
    newer_clicked = st.button("Newer (> center)")
    surprise_clicked = st.button("Surprise me")

    if older_clicked:
        st.session_state["year_filter"] = "older"
    if newer_clicked:
        st.session_state["year_filter"] = "newer"
    if surprise_clicked:
        st.session_state["year_filter"] = random.choice(["all", "older", "newer"])
        st.session_state["center_year"] = random.randint(1950, 2023)

year_filter = st.session_state.get("year_filter", "all")
center_year = st.session_state.get("center_year", 2000)


def parse_year(value: str) -> Optional[int]:
    match = re.search(r"(\d{4})", value or "")
    return int(match.group(1)) if match else None


def filter_by_year(detail: dict) -> bool:
    year = parse_year(detail.get("Year", ""))
    if year is None:
        return True
    if year_filter == "older":
        return year <= center_year
    if year_filter == "newer":
        return year > center_year
    return True


def gather_candidates(terms: List[str]) -> List[dict]:
    seen: Dict[str, dict] = {}
    for term in terms:
        if not term:
            continue
        for movie in search_movies(term):
            imdb_id = movie.get("imdbID")
            if imdb_id and imdb_id not in seen:
                seen[imdb_id] = movie
    return list(seen.values())


def matches_selection() -> List[dict]:
    query_terms = [genre_choice, director_choice] + selected_actors
    candidates = gather_candidates(query_terms)
    matches: List[dict] = []
    for movie in candidates:
        detail = fetch_movie_detail(movie.get("imdbID", ""))
        if not detail:
            continue
        genre_text = detail.get("Genre", "")
        director_text = detail.get("Director", "")
        actors_text = detail.get("Actors", "")

        if genre_choice.lower() not in genre_text.lower():
            continue
        if director_choice.lower() not in director_text.lower():
            continue
        if selected_actors and not all(actor.lower() in actors_text.lower() for actor in selected_actors):
            continue
        if not filter_by_year(detail):
            continue

        matches.append({"summary": movie, "detail": detail})
        if len(matches) == 10:
            break
    return matches


matches = matches_selection()

st.divider()
st.subheader("Matched Movies")

if not matches:
    st.info("No movies available for that combination right now.")
else:
    option_data = []
    for index, entry in enumerate(matches, start=1):
        detail = entry["detail"]
        title = detail.get("Title", "Unknown Title")
        year_text = detail.get("Year", "N/A")
        rating = detail.get("imdbRating", "N/A")
        label = f"{index}. {title} ({year_text}) — ⭐ {rating}"
        option_data.append((label, detail))

    labels = [label for label, _ in option_data]

    if st.session_state.get("match_choice") not in labels:
        st.session_state["match_choice"] = labels[0]

    selected_label = st.radio(
        "Select a movie to see the details",
        labels,
        key="match_choice",
    )

    selected_detail = next(detail for label, detail in option_data if label == selected_label)

    render_movie_detail(selected_detail)
