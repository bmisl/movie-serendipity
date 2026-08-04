"""Solo movie browser — Live streaming discovery (No local SQLite database needed).

Run with:
    .venv\\Scripts\\streamlit.exe run solo_browser_app.py
"""

import os
import time
from typing import Dict, List, Optional, Tuple

import pandas as pd
import requests
import streamlit as st
from st_aggrid import AgGrid, GridOptionsBuilder, ColumnsAutoSizeMode

from app_config import REGION_PROVIDERS, REGIONS, get_secret

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Solo Movie Browser",
    page_icon="🎬",
    layout="wide",
    initial_sidebar_state="expanded",
)

TMDB_BASE_URL = "https://api.themoviedb.org/3"

def resolve_tmdb_key() -> str:
    key = get_secret("TMDB_API_KEY")
    if not key:
        key = os.getenv("TMDB_API_KEY", "")
    return key

TMDB_API_KEY = resolve_tmdb_key()


def tmdb_api_get(path: str, params: Optional[Dict[str, object]] = None) -> dict:
    """Fetch JSON from TMDB API with timeout."""
    if not TMDB_API_KEY:
        st.error("Missing TMDB_API_KEY. Please configure `.streamlit/secrets.toml` or environment variable.")
        st.stop()

    payload: Dict[str, object] = {"api_key": TMDB_API_KEY, "language": "en-US"}
    if params:
        payload.update(params)

    try:
        response = requests.get(f"{TMDB_BASE_URL}{path}", params=payload, timeout=10)
        if response.status_code == 200:
            return response.json()
        return {}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Live Data Fetching & In-Memory Cache (No SQLite file needed)
# ---------------------------------------------------------------------------

@st.cache_data(ttl=3600, show_spinner=False)
def fetch_live_streaming_movies(region_code: str, service_ids: Tuple[int, ...], fetch_pages: int = 15) -> List[dict]:
    """Fetch popular movies currently available on streaming services in region_code.
    
    Returns a list of movie dicts with full details (credits, genres, overview, providers).
    Cached in memory for 1 hour.
    """
    if not service_ids:
        return []

    provider_ids = "|".join(str(pid) for pid in service_ids)
    discovered_movies: List[dict] = []
    seen_ids = set()

    for page in range(1, fetch_pages + 1):
        params = {
            "sort_by": "popularity.desc",
            "include_adult": "false",
            "page": page,
            "watch_region": region_code,
            "with_watch_providers": provider_ids,
            "with_ott_monetization_types": "flatrate|free|ads",
            "vote_count.gte": 5,
        }
        data = tmdb_api_get("/discover/movie", params)
        results = data.get("results", [])
        if not results:
            break

        for m in results:
            mid = m.get("id")
            if mid and mid not in seen_ids:
                seen_ids.add(mid)
                discovered_movies.append(m)

        time.sleep(0.1)

    # Fetch credits (actors/directors) for discovered movies
    movie_details: List[dict] = []
    # Create reverse map of provider_id to provider_name
    provider_id_to_name = {v: k for k, v in REGION_PROVIDERS.get(region_code, {}).items()}

    for m in discovered_movies:
        mid = m["id"]
        # Fetch details + credits
        detail = tmdb_api_get(f"/movie/{mid}", {"append_to_response": "credits,watch/providers"})
        if not detail:
            continue

        release_date = detail.get("release_date") or ""
        year = int(release_date[:4]) if len(release_date) >= 4 and release_date[:4].isdigit() else None

        genres_list = [g.get("name") for g in detail.get("genres", []) if g.get("name")]
        genres_str = ", ".join(sorted(genres_list))

        credits = detail.get("credits", {})
        cast = [c.get("name") for c in credits.get("cast", [])[:10] if c.get("name")]
        directors = [c.get("name") for c in credits.get("crew", []) if c.get("job") == "Director" and c.get("name")]

        # Determine services for this region
        p_results = detail.get("watch/providers", {}).get("results", {}).get(region_code, {})
        movie_services = set()
        for bucket in ("flatrate", "free", "ads"):
            for p in p_results.get(bucket, []) or []:
                pid = p.get("provider_id")
                name = provider_id_to_name.get(pid) or p.get("provider_name")
                if name:
                    movie_services.add(name)

        movie_details.append({
            "id": mid,
            "title": detail.get("title") or m.get("title", ""),
            "year": year,
            "rating": round(float(detail.get("vote_average", 0.0)), 1),
            "votes": int(detail.get("vote_count", 0)),
            "runtime": detail.get("runtime"),
            "overview": detail.get("overview", ""),
            "poster_path": detail.get("poster_path", ""),
            "genres": genres_str,
            "directors": ", ".join(directors),
            "actors": ", ".join(cast),
            "services": ", ".join(sorted(movie_services)),
        })
        time.sleep(0.1)

    return movie_details


# ---------------------------------------------------------------------------
# Sidebar — controls
# ---------------------------------------------------------------------------

with st.sidebar:
    st.title("🎬 Solo Movie Browser")
    st.markdown("Live streaming movie discovery (No database needed).")
    st.divider()

    region_name = st.selectbox("Country / Region", list(REGIONS.keys()), index=0)
    region_code = REGIONS[region_name]

    region_services_map = REGION_PROVIDERS.get(region_code, {})
    all_service_names = list(region_services_map.keys())

    selected_service_names = st.multiselect(
        "Streaming Services",
        options=all_service_names,
        default=all_service_names,
        help="Select which streaming services to search.",
    )

    fetch_limit = st.slider(
        "Catalogue size (movies to pull)",
        min_value=100,
        max_value=1000,
        value=300,
        step=100,
        help="Higher values pull more movies live from TMDB.",
    )

    if st.button("🔄 Refresh Live Data"):
        st.cache_data.clear()
        st.rerun()

    st.divider()
    st.caption("Live streaming data from TMDB (in-memory cached, no local DB).")

# ---------------------------------------------------------------------------
# Load Live Data into Pandas DataFrame
# ---------------------------------------------------------------------------

selected_service_ids = tuple(
    region_services_map[s] for s in selected_service_names if s in region_services_map
)
pages_to_fetch = max(1, fetch_limit // 20)

if not selected_service_ids:
    st.warning("Please select at least one streaming service in the sidebar.")
    st.stop()

with st.spinner(f"Pulling current streaming movies for {region_name} from TMDB…"):
    movies_list = fetch_live_streaming_movies(region_code, selected_service_ids, pages_to_fetch)

df = pd.DataFrame(movies_list)

if df.empty:
    st.info("No movies found for the selected services.")
    st.stop()

# Filter by selected services if needed
if selected_service_names and set(selected_service_names) != set(all_service_names):
    pattern = "|".join(selected_service_names)
    df = df[df["services"].str.contains(pattern, case=False, na=False)]

# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------

st.title("Movie Catalogue")
st.caption(
    f"Loaded **{len(df):,} movies** currently streaming in **{region_name}** on {', '.join(selected_service_names)}"
)

# ---------------------------------------------------------------------------
# AG Grid Table
# ---------------------------------------------------------------------------

DISPLAY_COLS = ["title", "year", "rating", "votes", "genres", "directors", "actors", "services"]

gb = GridOptionsBuilder.from_dataframe(df[DISPLAY_COLS])
gb.configure_column("title",     header_name="Title",     filter="agTextColumnFilter",   flex=3, minWidth=160)
gb.configure_column("year",      header_name="Year",      filter="agNumberColumnFilter", flex=1, minWidth=80,  type=["numericColumn"])
gb.configure_column("rating",    header_name="Rating",    filter="agNumberColumnFilter", flex=1, minWidth=90,  type=["numericColumn"])
gb.configure_column("votes",     header_name="Votes",     filter="agNumberColumnFilter", flex=1, minWidth=90,  type=["numericColumn"])
gb.configure_column("genres",    header_name="Genre",     filter="agTextColumnFilter",   flex=2, minWidth=140)
gb.configure_column("directors", header_name="Directors", filter="agTextColumnFilter",   flex=2, minWidth=140)
gb.configure_column("actors",    header_name="Actors",    filter="agTextColumnFilter",   flex=3, minWidth=180)
gb.configure_column("services",  header_name="Services",  filter="agTextColumnFilter",   flex=2, minWidth=140)

gb.configure_selection(selection_mode="single", use_checkbox=False)
gb.configure_default_column(resizable=True, sortable=True, floatingFilter=True)
gb.configure_grid_options(rowHeight=32, suppressMovableColumns=False)

grid_options = gb.build()

if "_selected_movie_id" not in st.session_state:
    st.session_state["_selected_movie_id"] = None

grid_response = AgGrid(
    df[DISPLAY_COLS],
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
selected_row = None
if selected_rows is not None:
    if hasattr(selected_rows, "empty"):
        if not selected_rows.empty:
            selected_row = selected_rows.iloc[0].to_dict()
    elif selected_rows:
        selected_row = selected_rows[0]

if selected_row is not None:
    clicked_title = selected_row.get("title", "")
    if clicked_title != st.session_state["_selected_movie_id"]:
        st.session_state["_selected_movie_id"] = clicked_title

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
                st.markdown(f"**{year}{runtime_str}** · ⭐ {rating}")

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

                services = full.get("services", "")
                if services:
                    st.markdown(f"📺 **Available on ({region_name}):** {services}")
                else:
                    st.markdown(f"📺 **Available on ({region_name}):** Not found on selected services")

        show_detail()
