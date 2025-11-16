"""Streamlit app for exploring the local TMDB-backed movie catalogue."""

import math
import sqlite3
from pathlib import Path
from typing import Iterable, List

import pandas as pd
import streamlit as st

from app_config import ensure_database_file

DB_PATH = "movies.sqlite"

st.set_page_config(page_title="🎞️ Movie Catalogue Explorer", layout="wide")

ensure_database_file(DB_PATH)


def split_multi_value(value: object) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = [part.strip() for part in value.split(",") if part and part.strip()]
    elif isinstance(value, Iterable):
        parts = []
        for item in value:
            if not item:
                continue
            parts.extend(split_multi_value(item))
    else:
        parts = []
    seen: set[str] = set()
    ordered: List[str] = []
    for part in parts:
        lowered = part.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        ordered.append(part)
    return ordered


def numeric_bounds(series: pd.Series, fallback_min: float, fallback_max: float) -> tuple[float, float]:
    values = pd.to_numeric(series, errors="coerce").dropna()
    if values.empty:
        return fallback_min, fallback_max
    minimum = float(values.min())
    maximum = float(values.max())
    if math.isfinite(minimum) and math.isfinite(maximum):
        return minimum, maximum
    return fallback_min, fallback_max


def gather_unique(series: pd.Series) -> List[str]:
    values: set[str] = set()
    for items in series:
        for item in items:
            values.add(item)
    return sorted(values)


@st.cache_data(show_spinner=False)
def load_movie_dataframe() -> tuple[pd.DataFrame, List[str]]:
    if not Path(DB_PATH).exists():
        return pd.DataFrame(), []

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        table_names = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        has_languages = "movie_languages" in table_names

        language_select = (
            "GROUP_CONCAT(DISTINCT ml.language_name) AS language_names"
            if has_languages
            else "NULL AS language_names"
        )
        language_join = (
            "LEFT JOIN movie_languages ml ON ml.movie_id = m.id"
            if has_languages
            else ""
        )

        query = f"""
            SELECT
                m.id AS tmdb_id,
                m.title,
                m.year,
                m.runtime,
                m.vote_average,
                m.vote_count,
                m.popularity,
                m.overview,
                m.collection_name,
                GROUP_CONCAT(DISTINCT CASE WHEN mp.role = 'Director' THEN p.name END) AS director_names,
                GROUP_CONCAT(DISTINCT CASE WHEN mp.role = 'Actor' THEN p.name END) AS actor_names,
                GROUP_CONCAT(DISTINCT mg.genre) AS genre_names,
                GROUP_CONCAT(DISTINCT c.name) AS company_names,
                {language_select}
            FROM movies m
            LEFT JOIN movie_people mp ON mp.movie_id = m.id
            LEFT JOIN people p ON p.id = mp.person_id
            LEFT JOIN movie_genres mg ON mg.movie_id = m.id
            LEFT JOIN movie_companies mc ON mc.movie_id = m.id
            LEFT JOIN companies c ON c.id = mc.company_id
            {language_join}
            GROUP BY m.id
            ORDER BY m.year DESC, m.popularity DESC
        """

        df = pd.read_sql_query(query, conn)

    df["genre_list"] = df["genre_names"].map(split_multi_value)
    df["director_list"] = df["director_names"].map(split_multi_value)
    df["actor_list"] = df["actor_names"].map(split_multi_value)
    df["company_list"] = df["company_names"].map(split_multi_value)
    df["language_list"] = df["language_names"].map(split_multi_value)

    df["Genres"] = df["genre_list"].map(lambda items: ", ".join(items))
    df["Directors"] = df["director_list"].map(lambda items: ", ".join(items))
    df["Actors"] = df["actor_list"].map(lambda items: ", ".join(items[:15]))
    df["Production Companies"] = df["company_list"].map(lambda items: ", ".join(items))
    df["Spoken Languages"] = df["language_list"].map(lambda items: ", ".join(items))

    df["Runtime (min)"] = pd.to_numeric(df["runtime"], errors="coerce").round().astype("Int64")
    df["Rating"] = pd.to_numeric(df["vote_average"], errors="coerce").round(2)
    df["Vote Count"] = pd.to_numeric(df["vote_count"], errors="coerce").astype("Int64")
    df["Popularity"] = pd.to_numeric(df["popularity"], errors="coerce").round(2)
    df["Year"] = pd.to_numeric(df["year"], errors="coerce").astype("Int64")

    df.rename(
        columns={
            "tmdb_id": "TMDB ID",
            "title": "Title",
            "overview": "Overview",
            "collection_name": "Collection",
        },
        inplace=True,
    )

    display_columns = [
        "TMDB ID",
        "Title",
        "Year",
        "Runtime (min)",
        "Rating",
        "Vote Count",
        "Popularity",
        "Genres",
        "Directors",
        "Actors",
        "Collection",
        "Production Companies",
        "Spoken Languages",
        "Overview",
    ]
    return df, display_columns


movies_df, display_columns = load_movie_dataframe()

st.title("🎞️ Movie Catalogue Explorer")
st.caption(
    "Browse and filter every movie you've collected locally. All filters apply in real time "
    "so you can quickly zero in on a specific cast, genre, or production detail."
)

if movies_df.empty:
    st.info("No movies found in the local database yet. Run the collection script to get started.")
    st.stop()

with st.sidebar:
    st.header("Filters")
    title_filter = st.text_input("Title contains")
    overview_filter = st.text_input("Overview contains")

    year_values = movies_df["Year"].dropna().astype(int)
    if year_values.empty:
        year_range = None
    else:
        year_min = int(year_values.min())
        year_max = int(year_values.max())
        if year_min < year_max:
            year_range = st.slider(
                "Release year",
                min_value=year_min,
                max_value=year_max,
                value=(year_min, year_max),
            )
        else:
            year_range = (year_min, year_max)

    rating_min, rating_max = numeric_bounds(movies_df["Rating"], 0.0, 10.0)
    if rating_max > rating_min:
        rating_min_bound = math.floor(rating_min * 10) / 10
        rating_max_bound = math.ceil(rating_max * 10) / 10
        rating_range = st.slider(
            "TMDB rating",
            min_value=float(rating_min_bound),
            max_value=float(rating_max_bound),
            value=(float(rating_min_bound), float(rating_max_bound)),
            step=0.1,
        )
    else:
        rating_range = (rating_min, rating_max)

    runtime_min, runtime_max = numeric_bounds(movies_df["Runtime (min)"], 0, 240)
    if runtime_max > runtime_min:
        runtime_range = st.slider(
            "Runtime (minutes)",
            min_value=int(runtime_min),
            max_value=int(runtime_max),
            value=(int(runtime_min), int(runtime_max)),
        )
    else:
        runtime_range = (runtime_min, runtime_max)

    vote_min, vote_max = numeric_bounds(movies_df["Vote Count"], 0, 1000)
    if vote_max > vote_min:
        vote_range = st.slider(
            "Vote count",
            min_value=int(vote_min),
            max_value=int(vote_max),
            value=(int(vote_min), int(vote_max)),
        )
    else:
        vote_range = (vote_min, vote_max)

    pop_min, pop_max = numeric_bounds(movies_df["Popularity"], 0.0, 500.0)
    if pop_max > pop_min:
        pop_min_bound = math.floor(pop_min * 10) / 10
        pop_max_bound = math.ceil(pop_max * 10) / 10
        popularity_range = st.slider(
            "Popularity",
            min_value=float(pop_min_bound),
            max_value=float(pop_max_bound),
            value=(float(pop_min_bound), float(pop_max_bound)),
            step=0.1,
        )
    else:
        popularity_range = (pop_min, pop_max)

    genre_filter = st.multiselect("Genres", gather_unique(movies_df["genre_list"]))
    director_filter = st.multiselect("Directors", gather_unique(movies_df["director_list"]))
    actor_filter = st.multiselect("Actors", gather_unique(movies_df["actor_list"]))
    company_filter = st.multiselect(
        "Production companies", gather_unique(movies_df["company_list"])
    )
    language_filter = st.multiselect(
        "Spoken languages", gather_unique(movies_df["language_list"])
    )
    collection_filter = st.multiselect(
        "Collections", sorted(value for value in movies_df["Collection"].dropna().unique())
    )

filtered = movies_df.copy()

if title_filter:
    filtered = filtered[filtered["Title"].str.contains(title_filter, case=False, na=False)]
if overview_filter:
    filtered = filtered[filtered["Overview"].str.contains(overview_filter, case=False, na=False)]

if year_range is not None:
    filtered = filtered[
        filtered["Year"].between(year_range[0], year_range[1], inclusive="both")
    ]

filtered = filtered[
    (filtered["Rating"].fillna(0) >= rating_range[0])
    & (filtered["Rating"].fillna(0) <= rating_range[1])
]
filtered = filtered[
    (filtered["Runtime (min)"].fillna(runtime_min) >= runtime_range[0])
    & (filtered["Runtime (min)"].fillna(runtime_min) <= runtime_range[1])
]
filtered = filtered[
    (filtered["Vote Count"].fillna(vote_min) >= vote_range[0])
    & (filtered["Vote Count"].fillna(vote_min) <= vote_range[1])
]
filtered = filtered[
    (filtered["Popularity"].fillna(pop_min) >= popularity_range[0])
    & (filtered["Popularity"].fillna(pop_min) <= popularity_range[1])
]

if genre_filter:
    filtered = filtered[
        filtered["genre_list"].map(lambda values: all(g in values for g in genre_filter))
    ]
if director_filter:
    filtered = filtered[
        filtered["director_list"].map(lambda values: all(d in values for d in director_filter))
    ]
if actor_filter:
    filtered = filtered[
        filtered["actor_list"].map(lambda values: all(a in values for a in actor_filter))
    ]
if company_filter:
    filtered = filtered[
        filtered["company_list"].map(lambda values: all(c in values for c in company_filter))
    ]
if language_filter:
    filtered = filtered[
        filtered["language_list"].map(lambda values: all(l in values for l in language_filter))
    ]
if collection_filter:
    filtered = filtered[
        filtered["Collection"].fillna("").isin(collection_filter)
    ]

st.write(
    f"Showing {len(filtered)} of {len(movies_df)} movies after applying the current filters."
)

st.dataframe(
    filtered[display_columns],
    width="stretch",
    hide_index=True,
)
