"""Solo movie browser — find something to watch tonight.

Run with:
    .venv\\Scripts\\streamlit.exe run solo_browser_app.py
"""

import sqlite3
from typing import List, Optional

import pandas as pd
import streamlit as st
from st_aggrid import AgGrid, GridOptionsBuilder, ColumnsAutoSizeMode

from app_config import DB_PATH, REGION_PROVIDERS, REGIONS

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Solo Browser",
    page_icon="🎬",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

@st.cache_data(ttl=300)
def load_movies(region: str, service_ids: tuple) -> pd.DataFrame:
    """Load movies from SQLite, optionally filtered to services in the given region."""
    conn = sqlite3.connect(DB_PATH)

    # Build the provider filter clause
    if service_ids:
        placeholders = ",".join("?" * len(service_ids))
        provider_filter = f"""
            AND m.id IN (
                SELECT DISTINCT movie_id FROM movie_providers
                WHERE region = ? AND provider_id IN ({placeholders})
                AND provider_type IN ('flatrate', 'free', 'ads')
            )
        """
        filter_params: List = [region] + list(service_ids)
    else:
        provider_filter = ""
        filter_params = []

    query = f"""
        SELECT
            m.id,
            m.title,
            CAST(m.year AS INTEGER)          AS year,
            ROUND(m.vote_average, 1)         AS rating,
            m.vote_count                     AS votes,
            m.runtime,
            m.overview,
            m.poster_path,
            GROUP_CONCAT(DISTINCT mg.genre)  AS genres,
            GROUP_CONCAT(DISTINCT CASE WHEN mp.role = 'Director' THEN p.name END) AS directors,
            GROUP_CONCAT(DISTINCT CASE WHEN mp.role = 'Actor'    THEN p.name END) AS actors
        FROM movies m
        LEFT JOIN movie_genres mg ON mg.movie_id = m.id
        LEFT JOIN movie_people mp ON mp.movie_id = m.id
        LEFT JOIN people p        ON p.id = mp.person_id
        WHERE m.title IS NOT NULL
          AND m.year IS NOT NULL
          {provider_filter}
        GROUP BY m.id
        ORDER BY m.popularity DESC
    """

    df = pd.read_sql_query(query, conn, params=filter_params)
    conn.close()

    # Clean up aggregated strings — sort them alphabetically for readability
    for col in ("genres", "directors", "actors"):
        df[col] = df[col].fillna("").apply(
            lambda v: ", ".join(sorted(x.strip() for x in v.split(",") if x.strip()))
        )

    return df


@st.cache_data(ttl=300)
def load_services_for_movie(movie_id: int, region: str) -> List[str]:
    """Return the streaming service names for a movie in the given region."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT DISTINCT provider_name FROM movie_providers
        WHERE movie_id = ? AND region = ?
          AND provider_type IN ('flatrate', 'free', 'ads')
        ORDER BY provider_name
        """,
        (movie_id, region),
    )
    names = [row[0] for row in cur.fetchall()]
    conn.close()
    return names


# ---------------------------------------------------------------------------
# Sidebar — region & service selection
# ---------------------------------------------------------------------------

with st.sidebar:
    st.title("🎬 Solo Browser")
    st.markdown("Find a movie to watch tonight.")
    st.divider()

    region_name = st.selectbox("Region", list(REGIONS.keys()), index=0)
    region_code = REGIONS[region_name]

    region_services = REGION_PROVIDERS.get(region_code, {})
    all_service_names = list(region_services.keys())

    st.markdown("**Streaming services**")
    show_all = st.toggle("Show all movies (ignore services)", value=False)

    if show_all:
        selected_services: List[str] = []
    else:
        selected_services = st.multiselect(
            "Filter by service",
            options=all_service_names,
            default=all_service_names,
            label_visibility="collapsed",
        )

    st.divider()
    st.caption(
        "Provider data is fetched from TMDB and cached locally. "
        "Re-run `backfill_providers.py` to refresh."
    )

# ---------------------------------------------------------------------------
# Load data
# ---------------------------------------------------------------------------

selected_ids = tuple(
    region_services[s] for s in selected_services if s in region_services
)

with st.spinner("Loading movies…"):
    df = load_movies(region_code, selected_ids)

# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------

st.title("Movie Browser")
if show_all:
    st.caption(f"Showing all {len(df):,} movies in the database.")
elif selected_services:
    service_label = ", ".join(selected_services)
    st.caption(
        f"Showing {len(df):,} movies available in **{region_name}** on: {service_label}"
    )
else:
    st.caption("No services selected — select at least one service in the sidebar, or toggle 'Show all movies'.")

# ---------------------------------------------------------------------------
# AG Grid table
# ---------------------------------------------------------------------------

DISPLAY_COLS = ["title", "year", "rating", "votes", "genres", "directors", "actors"]
COL_LABELS = {
    "title": "Title",
    "year": "Year",
    "rating": "Rating",
    "votes": "Votes",
    "genres": "Genre",
    "directors": "Directors",
    "actors": "Actors",
}

grid_df = df[DISPLAY_COLS + ["id", "overview", "poster_path", "runtime"]].copy()

gb = GridOptionsBuilder.from_dataframe(grid_df[DISPLAY_COLS])

# Column definitions
gb.configure_column("title",     header_name="Title",     filter="agTextColumnFilter",   flex=3, minWidth=160)
gb.configure_column("year",      header_name="Year",      filter="agNumberColumnFilter", flex=1, minWidth=80,  type=["numericColumn"])
gb.configure_column("rating",    header_name="Rating",    filter="agNumberColumnFilter", flex=1, minWidth=90,  type=["numericColumn"])
gb.configure_column("votes",     header_name="Votes",     filter="agNumberColumnFilter", flex=1, minWidth=90,  type=["numericColumn"])
gb.configure_column("genres",    header_name="Genre",     filter="agTextColumnFilter",   flex=2, minWidth=140)
gb.configure_column("directors", header_name="Directors", filter="agTextColumnFilter",   flex=2, minWidth=140)
gb.configure_column("actors",    header_name="Actors",    filter="agTextColumnFilter",   flex=3, minWidth=180)

gb.configure_selection(selection_mode="single", use_checkbox=False)
gb.configure_default_column(resizable=True, sortable=True, floatingFilter=True)
gb.configure_grid_options(rowHeight=32, suppressMovableColumns=False)

grid_options = gb.build()

# Initialise selection tracker — only show dialog when the user clicks a NEW row
if "_selected_movie_id" not in st.session_state:
    st.session_state["_selected_movie_id"] = None

grid_response = AgGrid(
    grid_df[DISPLAY_COLS],
    gridOptions=grid_options,
    update_on=["selectionChanged"],
    columns_auto_size_mode=ColumnsAutoSizeMode.FIT_CONTENTS,
    height=600,
    allow_unsafe_jscode=False,
    theme="streamlit",
)

# ---------------------------------------------------------------------------
# Detail dialog on row selection
# ---------------------------------------------------------------------------

selected_rows = grid_response.get("selected_rows")

# Normalise selected_rows to a plain dict (or None)
selected_row = None
if selected_rows is not None:
    if hasattr(selected_rows, "empty"):
        if not selected_rows.empty:
            selected_row = selected_rows.iloc[0].to_dict()
    elif selected_rows:
        selected_row = selected_rows[0]

# Only open the dialog when the user genuinely clicks a new row.
# Typing in the filter re-renders the grid and re-sends the old selection —
# we detect this by comparing the movie title against what we already showed.
if selected_row is not None:
    clicked_title = selected_row.get("title", "")
    if clicked_title != st.session_state["_selected_movie_id"]:
        st.session_state["_selected_movie_id"] = clicked_title

        # Look up the full record (includes poster_path, overview, etc.)
        matches = df[df["title"] == clicked_title]
        full = matches.iloc[0] if not matches.empty else pd.Series(selected_row)

        @st.dialog(str(full.get("title", "Movie Detail")), width="large")
        def show_detail():
            col_poster, col_info = st.columns([1, 2])

            with col_poster:
                poster = full.get("poster_path", "")
                if poster:
                    st.image(f"https://image.tmdb.org/t/p/w300{poster}")

            with col_info:
                year = full.get("year", "")
                runtime = full.get("runtime", "")
                rating = full.get("rating", "")
                runtime_str = f" · {runtime} min" if runtime else ""
                st.markdown(f"**{year}{runtime_str}** · \u2b50 {rating}")

                genres = full.get("genres", "")
                if genres:
                    st.markdown(f"*{genres}*")

                overview = full.get("overview", "")
                if overview:
                    st.markdown(overview)

                st.divider()

                directors = full.get("directors", "")
                if directors:
                    st.markdown(f"**Director(s):** {directors}")

                actors = full.get("actors", "")
                if actors:
                    st.markdown(f"**Cast:** {actors}")

                st.divider()

                movie_id = full.get("id")
                if movie_id:
                    services = load_services_for_movie(int(movie_id), region_code)
                    if services:
                        st.markdown(f"**Available on ({region_name}):** {', '.join(services)}")
                    else:
                        st.markdown(f"**Available on ({region_name}):** Not found on any tracked service")

        show_detail()

