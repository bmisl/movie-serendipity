import random
from typing import List, Optional

import requests
import streamlit as st
import streamlit.components.v1 as components

# Fetch TMDB API key from secrets
try:
    TMDB_API_KEY = st.secrets["TMDB_API_KEY"]
except Exception:
    import os

    TMDB_API_KEY = os.getenv("TMDB_API_KEY", "")

TMDB_BASE_URL = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w500"
RANK_BATCH_SIZE = 24
LIST_BATCH_SIZE = 50
MATCH_POOL_SIZE = 120

REGION_PROVIDERS = {
    "FI": {
        "Netflix": 8, "Amazon Prime Video": 119, "Disney Plus": 337,
        "HBO Max": 1899, "Viaplay": 76, "Apple TV+": 350,
        "Ruutu": 338, "Yle Areena": 323, "Viddla": 539
    },
    "DK": {
        "Netflix": 8, "Amazon Prime Video": 119, "Disney Plus": 337,
        "HBO Max": 1899, "Viaplay": 76, "Apple TV+": 350,
        "TV 2 Play": 398, "DRTV": 620
    },
    "IS": {
        "Netflix": 8, "Amazon Prime Video": 119, "Disney Plus": 337,
        "HBO Max": 1899, "Viaplay": 76, "Apple TV+": 350,
        "RÚV": 2674
    }
}

GENRES = {
    "All": None,
    "Action": 28,
    "Adventure": 12,
    "Animation": 16,
    "Comedy": 35,
    "Crime": 80,
    "Documentary": 99,
    "Drama": 18,
    "Fantasy": 14,
    "Horror": 27,
    "Romance": 10749,
    "Sci-Fi": 878,
    "Thriller": 53,
}

REGIONS = {
    "Finland": "FI",
    "Denmark": "DK",
    "Iceland": "IS",
}


@st.cache_resource
def get_global_session() -> dict:
    return {
        "users": {},
        "genre": None,
        "movies": [],
        "movie_pool": [],
        "movie_cursor": 0,
        "mode": "rating",
        "state": "SETUP",
        "match": None,
        "region": "FI",
    }


lobby = get_global_session()


def _safe_float(value, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def sort_movies_by_popularity(movies: List[dict]) -> List[dict]:
    return sorted(
        movies,
        key=lambda movie: (
            _safe_float(movie.get("popularity"), -1.0),
            _safe_float(movie.get("vote_count"), -1.0),
            _safe_float(movie.get("vote_average"), -1.0),
        ),
        reverse=True,
    )


def build_discover_params(genre_id: Optional[int], provider_ids: List[int], page: int, region: str = "FI") -> dict:
    params = {
        "api_key": TMDB_API_KEY,
        "language": "en-US",
        "watch_region": region,
        "region": region, # Restricts release dates to the selected region
        "sort_by": "popularity.desc",
        "page": page,
    }
    if genre_id is not None:
        params["with_genres"] = genre_id
    if provider_ids:
        params["with_watch_providers"] = "|".join(map(str, provider_ids))
    return params


@st.cache_data(show_spinner=False, ttl=3600)
def fetch_ranked_movies(genre_id: Optional[int], provider_ids: tuple[int, ...], limit: int, region: str = "FI") -> List[dict]:
    movies: List[dict] = []
    page = 1
    total_pages: Optional[int] = None

    seen_ids: set[int] = set()
    while len(movies) < limit and (total_pages is None or page <= total_pages) and page <= 25:
        res = requests.get(f"{TMDB_BASE_URL}/discover/movie", params=build_discover_params(genre_id, list(provider_ids), page, region))
        if res.status_code != 200:
            break
        payload = res.json()
        for movie in payload.get("results", []):
            if movie["id"] not in seen_ids:
                seen_ids.add(movie["id"])
                movies.append(movie)
        total_pages = int(payload.get("total_pages") or page)
        if not payload.get("results"):
            break
        page += 1

    return sort_movies_by_popularity(movies)[:limit]


def get_combined_provider_ids() -> tuple[int, ...]:
    combined_services = set()
    for user in lobby["users"].values():
        combined_services.update(user["services"])
    region_code = lobby.get("region", "FI")
    providers_map = REGION_PROVIDERS.get(region_code, REGION_PROVIDERS["FI"])
    return tuple(sorted(providers_map[service] for service in combined_services if service in providers_map))


def get_combined_service_names() -> List[str]:
    combined_services = set()
    for user in lobby["users"].values():
        combined_services.update(user["services"])
    return sorted(combined_services)


def get_movie_pool(genre_name: Optional[str], limit: int = MATCH_POOL_SIZE) -> List[dict]:
    genre_id = GENRES[genre_name] if genre_name else None
    return fetch_ranked_movies(genre_id, get_combined_provider_ids(), limit, lobby.get("region", "FI"))


def reset_lobby() -> None:
    lobby.clear()
    lobby.update(
        {
            "users": {},
            "genre": None,
            "movies": [],
            "movie_pool": [],
            "movie_cursor": 0,
            "mode": "rating",
            "state": "SETUP",
            "match": None,
                "region": "FI",
        }
    )
    st.session_state.user_name = None
    for key in ("join_name", "join_services", "reset_confirmation"):
        if key in st.session_state:
            del st.session_state[key]


def leave_lobby(user_name: str) -> None:
    """Remove a single user from the lobby without resetting everyone else."""
    lobby["users"].pop(user_name, None)
    st.session_state.user_name = None
    for key in ("join_name", "join_services"):
        if key in st.session_state:
            del st.session_state[key]


def load_next_movie_batch() -> None:
    next_cursor = lobby.get("movie_cursor", 0) + RANK_BATCH_SIZE
    pool = lobby.get("movie_pool", [])
    if next_cursor + RANK_BATCH_SIZE > len(pool):
        pool = get_movie_pool(lobby["genre"], max(MATCH_POOL_SIZE, next_cursor + RANK_BATCH_SIZE))
        lobby["movie_pool"] = pool
    lobby["movie_cursor"] = next_cursor
    lobby["movies"] = pool[next_cursor: next_cursor + RANK_BATCH_SIZE]
    for user in lobby["users"].values():
        user["votes"] = {}
        user["round2_votes"] = {}
        user["ready"] = False
    lobby["state"] = "RATING"
    st.rerun()


def movie_batch_has_votes() -> bool:
    return any(
        any(user["votes"].get(movie["id"], 0) > 0 for user in lobby["users"].values())
        for movie in lobby["movies"]
    )


def start_matching(mode: str, genre_name: Optional[str]) -> bool:
    lobby["genre"] = genre_name
    lobby["movie_cursor"] = 0
    lobby["movie_pool"] = get_movie_pool(genre_name, MATCH_POOL_SIZE)
    lobby["mode"] = mode
    lobby["movies"] = lobby["movie_pool"][:RANK_BATCH_SIZE]
    lobby["state"] = "RATING"
    if lobby["movies"]:
        st.rerun()
        return True
    return False


def start_browsing(genre_name: Optional[str]) -> bool:
    lobby["genre"] = genre_name
    lobby["movie_cursor"] = 0
    lobby["movie_pool"] = get_movie_pool(genre_name, 96)
    lobby["state"] = "BROWSE"
    if lobby["movie_pool"]:
        st.rerun()
        return True
    return False


def load_prev_browse_page() -> None:
    curr_cursor = lobby.get("movie_cursor", 0)
    lobby["movie_cursor"] = max(0, curr_cursor - 96)
    st.rerun()


def load_next_browse_page() -> None:
    next_cursor = lobby.get("movie_cursor", 0) + 96
    pool = lobby.get("movie_pool", [])
    if next_cursor + 96 > len(pool):
        new_limit = len(pool) + 96
        pool = get_movie_pool(lobby["genre"], new_limit)
        lobby["movie_pool"] = pool
    lobby["movie_cursor"] = next_cursor
    st.rerun()


def auto_refresh_page(interval_ms: int = 5000) -> None:
    components.html(
        f"""
        <script>
        setTimeout(function() {{
            const doc = window.parent.document;
            const buttons = Array.from(doc.querySelectorAll('button'));
            const refreshBtn = buttons.find(b => b.innerText.includes('HiddenAutoRefresh'));
            if (refreshBtn) {{
                refreshBtn.click();
            }}
        }}, {interval_ms});
        </script>
        """,
        height=0,
        width=0,
    )


def current_movie_batch() -> List[dict]:
    pool = lobby.get("movie_pool", [])
    cursor = int(lobby.get("movie_cursor", 0) or 0)
    batch = pool[cursor: cursor + RANK_BATCH_SIZE]
    if batch:
        return batch
    return sort_movies_by_popularity(lobby.get("movies", []))


def apply_rank_selection(movie_id: int, user_name: str) -> None:
    user = lobby["users"].get(user_name)
    if not user:
        return

    key = f"rate_{movie_id}"
    selected_rank = st.session_state.get(key)
    try:
        selected_rank = int(selected_rank) if selected_rank is not None else 0
    except (TypeError, ValueError):
        selected_rank = 0

    if selected_rank not in {1, 2, 3, 4, 5}:
        user["votes"][movie_id] = 0
        return

    for other_movie_id, other_rank in list(user["votes"].items()):
        if other_movie_id != movie_id and other_rank == selected_rank:
            user["votes"][other_movie_id] = 0
            other_key = f"rate_{other_movie_id}"
            if other_key in st.session_state:
                st.session_state[other_key] = None

    user["votes"][movie_id] = selected_rank


@st.cache_data(show_spinner=False, ttl=3600)
def fetch_movie_watch_providers(movie_id: int, region: str = "FI") -> List[str]:
    try:
        response = requests.get(f"{TMDB_BASE_URL}/movie/{movie_id}/watch/providers", params={"api_key": TMDB_API_KEY})
        if response.status_code != 200:
            return []
        payload = response.json().get("results", {}).get(region, {})
        provider_names: List[str] = []
        provider_id_to_name = {v: k for k, v in REGION_PROVIDERS.get(region, {}).items()}
        for bucket in ("flatrate", "free", "ads", "buy", "rent"):
            for provider in payload.get(bucket, []) or []:
                pid = provider.get("provider_id")
                name = provider_id_to_name.get(pid)
                if name and name not in provider_names:
                    provider_names.append(name)
        return provider_names
    except Exception:
        return []


st.set_page_config(page_title="WatchMatch", layout="wide", initial_sidebar_state="collapsed")

# Theme Selection Configuration
theme_style = st.session_state.get("sidebar_theme_style", "Netflix (Red)")

theme_configs = {
    "Netflix (Red)": {
        "bg": "radial-gradient(circle at top, #1c1010 0%, #0c0707 100%)",
        "accent": "#e50914",
        "accent_glow": "rgba(229, 9, 20, 0.4)",
        "accent_glow_hover": "rgba(229, 9, 20, 0.6)",
        "card_border_hover": "rgba(229, 9, 20, 0.4)",
        "card_shadow_hover": "rgba(229, 9, 20, 0.2)",
        "button_gradient": "linear-gradient(135deg, #e50914 0%, #b81d24 100%)"
    },
    "HBO Max (Purple)": {
        "bg": "radial-gradient(circle at top, #161026 0%, #0a0712 100%)",
        "accent": "#9933ff",
        "accent_glow": "rgba(153, 51, 255, 0.4)",
        "accent_glow_hover": "rgba(153, 51, 255, 0.6)",
        "card_border_hover": "rgba(153, 51, 255, 0.4)",
        "card_shadow_hover": "rgba(153, 51, 255, 0.2)",
        "button_gradient": "linear-gradient(135deg, #9933ff 0%, #6600cc 100%)"
    },
    "Prime Video (Blue)": {
        "bg": "radial-gradient(circle at top, #0f1721 0%, #05080c 100%)",
        "accent": "#00a8e1",
        "accent_glow": "rgba(0, 168, 225, 0.4)",
        "accent_glow_hover": "rgba(0, 168, 225, 0.6)",
        "card_border_hover": "rgba(0, 168, 225, 0.4)",
        "card_shadow_hover": "rgba(0, 168, 225, 0.2)",
        "button_gradient": "linear-gradient(135deg, #00a8e1 0%, #007eb9 100%)"
    },
    "Apple TV+ (Glass)": {
        "bg": "radial-gradient(circle at top, #1e1e1e 0%, #0c0c0c 100%)",
        "accent": "#ffffff",
        "accent_glow": "rgba(255, 255, 255, 0.3)",
        "accent_glow_hover": "rgba(255, 255, 255, 0.5)",
        "card_border_hover": "rgba(255, 255, 255, 0.4)",
        "card_shadow_hover": "rgba(255, 255, 255, 0.15)",
        "button_gradient": "linear-gradient(135deg, #555555 0%, #222222 100%)"
    }
}

cfg = theme_configs.get(theme_style, theme_configs["Netflix (Red)"])

# Inject Custom Cinematic Dark CSS
st.markdown(
    f"""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700&display=swap');
    
    /* Apply premium typography and background */
    html, body, [data-testid="stAppViewContainer"] {{
        font-family: 'Outfit', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
        background: {cfg['bg']} !important;
        color: #f5f5f7 !important;
    }}
    
    /* Clean up top padding and hide default streamlit header/footer */
    [data-testid="stHeader"], footer {{
        display: none !important;
    }}
    
    /* Main block padding adjustment */
    [data-testid="stAppViewBlockContainer"] {{
        padding-top: 2rem !important;
        padding-bottom: 5rem !important;
    }}
    
    /* Premium Title Header styling */
    h1, h2, h3, h4, h5, h6 {{
        font-family: 'Outfit', sans-serif;
        font-weight: 600 !important;
        letter-spacing: -0.02em;
        color: #ffffff !important;
    }}
    
    /* Subtitle styling */
    div[data-testid="stMarkdownContainer"] p {{
        color: #a1a1a6;
        font-size: 1.05rem;
    }}
    
    /* Customize Streamlit Buttons */
    /* Primary buttons (Vibrant gradient style) */
    div[data-testid="stButton"] button[kind="primary"] {{
        background: {cfg['button_gradient']} !important;
        border: none !important;
        color: white !important;
        font-weight: 600 !important;
        border-radius: 8px !important;
        box-shadow: 0 4px 15px {cfg['accent_glow']} !important;
        transition: all 0.25s cubic-bezier(0.4, 0, 0.2, 1) !important;
        padding: 0.5rem 1.5rem !important;
        width: 100%;
    }}
    div[data-testid="stButton"] button[kind="primary"]:hover {{
        transform: translateY(-2px) !important;
        box-shadow: 0 6px 20px {cfg['accent_glow_hover']} !important;
    }}
    
    /* Secondary/standard buttons (Apple TV+ Glass style) */
    div[data-testid="stButton"] button[kind="secondary"] {{
        background: rgba(255, 255, 255, 0.08) !important;
        border: 1px solid rgba(255, 255, 255, 0.12) !important;
        color: #ffffff !important;
        border-radius: 8px !important;
        font-weight: 500 !important;
        transition: all 0.25s cubic-bezier(0.4, 0, 0.2, 1) !important;
        backdrop-filter: blur(8px);
        width: 100%;
    }}
    div[data-testid="stButton"] button[kind="secondary"]:hover {{
        background: rgba(255, 255, 255, 0.15) !important;
        border-color: rgba(255, 255, 255, 0.25) !important;
        transform: translateY(-1px) !important;
    }}
    
    /* Movie Cards Container */
    div[data-testid="column"]:has(.movie-card-marker) {{
        background: rgba(255, 255, 255, 0.03) !important;
        border: 1px solid rgba(255, 255, 255, 0.05) !important;
        border-radius: 16px !important;
        padding: 16px !important;
        box-shadow: 0 10px 30px rgba(0, 0, 0, 0.4) !important;
        backdrop-filter: blur(12px);
        transition: all 0.3s cubic-bezier(0.25, 0.8, 0.25, 1) !important;
    }}
    div[data-testid="column"]:has(.movie-card-marker):hover {{
        transform: translateY(-6px) !important;
        border-color: {cfg['card_border_hover']} !important;
        box-shadow: 0 15px 35px {cfg['card_shadow_hover']} !important;
        background: rgba(255, 255, 255, 0.05) !important;
    }}
    
    /* Movie card image/poster styling */
    div[data-testid="column"] img {{
        border-radius: 12px !important;
        box-shadow: 0 8px 24px rgba(0, 0, 0, 0.3) !important;
        transition: transform 0.3s ease !important;
    }}
    
    /* Input selection containers (Glassmorphism inputs) */
    div[data-baseweb="select"] > div {{
        background-color: rgba(255, 255, 255, 0.04) !important;
        border: 1px solid rgba(255, 255, 255, 0.08) !important;
        border-radius: 8px !important;
        color: white !important;
    }}
    
    div[data-baseweb="select"] span {{
        color: white !important;
    }}
    
    /* Segmented Control / Popcorn buttons styling */
    div[data-testid="stSegmentedControl"] {{
        background: rgba(0, 0, 0, 0.25) !important;
        border-radius: 8px !important;
        border: 1px solid rgba(255, 255, 255, 0.08) !important;
        padding: 2px !important;
    }}
    
    div[data-testid="stSegmentedControl"] button {{
        background: transparent !important;
        border: none !important;
        color: #a1a1a6 !important;
        border-radius: 6px !important;
        transition: all 0.2s ease !important;
    }}
    
    div[data-testid="stSegmentedControl"] button[aria-checked="true"] {{
        background: rgba(255, 255, 255, 0.12) !important;
        color: #ffffff !important;
        box-shadow: 0 2px 8px rgba(0, 0, 0, 0.2) !important;
    }}
    
    /* Sidebar custom styling */
    section[data-testid="stSidebar"] {{
        background-color: #0b0b0f !important;
        border-right: 1px solid rgba(255, 255, 255, 0.05) !important;
    }}
    
    /* Expander styling */
    div[data-testid="stExpander"] {{
        background: rgba(255, 255, 255, 0.02) !important;
        border: 1px solid rgba(255, 255, 255, 0.04) !important;
        border-radius: 8px !important;
    }}
    
    /* Dialog / Modal style */
    div[data-testid="stDialog"] {{
        background: #0f0f15 !important;
        border: 1px solid rgba(255, 255, 255, 0.08) !important;
        border-radius: 20px !important;
        box-shadow: 0 20px 60px rgba(0, 0, 0, 0.6) !important;
    }}
    
    /* Dataframe tables custom background for dark theme */
    div[data-testid="stDataFrame"] {{
        background-color: rgba(255, 255, 255, 0.02) !important;
        border: 1px solid rgba(255, 255, 255, 0.05) !important;
        border-radius: 12px !important;
        overflow: hidden;
    }}
    </style>
    """,
    unsafe_allow_html=True
)


@st.dialog("Help & Instructions")
def show_help_dialog():
    st.markdown(
        """
    **Welcome to WatchMatch!**
    - **Step 1:** Enter your name and select your streaming services.
    - **Step 2:** Wait for your friends to join the same screen.
    - **Step 3:** Pick a genre (or select 'All') and pick one of the modes:

    ### 🎬 Modes
    - **Movie Ranking (Social Match):** Choose up to 5 movies from the 24-card list and assign unique ranks from 1 to 5 to any subset you like. The top-ranked movies are shown for a final vote. If everyone votes Yes, it's a match!
    - **Movie List (Quick Browse):** Quickly browse movies across the selected services and genre in a large 6x16 grid (96 movies at a time). Perfect for single-user discovery.

    ---
    
    *Keyboard Shortcuts:*
    - **H**: Show this help menu.
    - **L**: Open the ranked movie list for the active genre.
    - **R**: Open the reset dialog.
    - **Spacebar**: Trigger a manual status refresh.
    """
    )


if st.button("HiddenHelp"):
    show_help_dialog()


@st.dialog("Ranked Movie List", width="large")
def show_movie_list_dialog():
    region = lobby.get("region", "FI")
    provider_ids = get_combined_provider_ids()
    sort_by = st.radio("Sort by", ["📈 Popularity", "⭐ TMDB Rating"], horizontal=True)

    with st.spinner("Fetching ranked movies and checking streaming services..."):
        try:
            genre_id = GENRES[lobby["genre"]] if lobby.get("genre") else None
            ranked_movies = fetch_ranked_movies(genre_id, provider_ids, LIST_BATCH_SIZE, region)
        except Exception:
            ranked_movies = []

        if not ranked_movies:
            st.info("No movies found for the selected criteria and streaming services.")
            return

        if "TMDB Rating" in sort_by:
            ranked_movies = sorted(ranked_movies, key=lambda m: _safe_float(m.get("vote_average"), -1), reverse=True)

        shared_services = get_combined_service_names()
        rows = []
        for rank, movie in enumerate(ranked_movies, start=1):
            # Resolve one available service for the movie
            providers = fetch_movie_watch_providers(movie["id"], region)
            available_on = [p for p in providers if p in shared_services]
            service = ", ".join(available_on) if available_on else "N/A"

            rows.append(
                {
                    "Rank": rank,
                    "Title": movie.get("title", "Untitled"),
                    "Streaming Service": service,
                    "Popularity": round(_safe_float(movie.get("popularity")), 2),
                    "TMDB Rating": movie.get("vote_average", "N/A"),
                    "Year": (movie.get("release_date", "") or "")[:4],
                }
            )

    sort_label = "TMDB Rating" if "TMDB Rating" in sort_by else "Popularity"
    genre_text = f"'{lobby['genre']}'" if lobby.get("genre") else "Any Genre"
    st.caption(f"Showing the top {min(LIST_BATCH_SIZE, len(ranked_movies))} movies sorted by {sort_label} for {genre_text} and shared streaming services.")
    st.dataframe(rows, use_container_width=True, hide_index=True)


@st.dialog("Reset WatchMatch")
def show_reset_dialog():
    st.warning("This clears all active users, votes, and the current match.")
    confirmation = st.text_input("Type reset to confirm", key="reset_confirmation")
    if st.button("Reset everything", type="primary"):
        if confirmation.strip().lower() == "reset":
            reset_lobby()
            st.rerun()
        else:
            st.error("Type reset exactly to confirm.")


if st.button("HiddenMovieList"):
    show_movie_list_dialog()


if st.button("HiddenReset"):
    show_reset_dialog()

if st.button("HiddenAutoRefresh"):
    st.rerun()


components.html(
    """
    <script>
    function bindShortcuts(doc) {
        if (!doc) return;
        if (doc.window_watchmatch_keys_bound) {
            console.log("WatchMatch: Keys already bound to", doc.title || "document");
            return;
        }

        const handleKey = function(e) {
            const targetTag = (e.target && e.target.tagName) ? e.target.tagName : "";
            if (targetTag === 'INPUT' || targetTag === 'TEXTAREA' || targetTag === 'SELECT') return;

            const key = (e.key || '').toLowerCase();
            const buttons = Array.from(doc.querySelectorAll('button'));

            let handled = false;

            if (key === ' ' || e.code === 'Space' || e.key === 'Spacebar') {
                const refreshBtn = buttons.find(b => b.innerText.includes('HiddenAutoRefresh'));
                if (refreshBtn) {
                    refreshBtn.click();
                    handled = true;
                }
            } else if (key === 'h') {
                const hBtn = buttons.find(b => b.innerText.includes('HiddenHelp'));
                if (hBtn) {
                    hBtn.click();
                    handled = true;
                }
            } else if (key === 'l') {
                const lBtn = buttons.find(b => b.innerText.includes('HiddenMovieList'));
                if (lBtn) {
                    lBtn.click();
                    handled = true;
                }
            } else if (key === 'r') {
                const rBtn = buttons.find(b => b.innerText.includes('HiddenReset'));
                if (rBtn) {
                    rBtn.click();
                    handled = true;
                }
            } else if (key === 'd') {
                const toggleLabel = Array.from(doc.querySelectorAll('label')).find(el => el.innerText && el.innerText.toLowerCase().includes('dark mode'));
                if (toggleLabel) {
                    toggleLabel.click();
                    handled = true;
                } else {
                    doc.documentElement.classList.toggle('custom-dark-mode');
                    handled = true;
                }
            }

            if (handled) {
                e.preventDefault();
                e.stopPropagation();
            }
        };

        doc.addEventListener('keydown', handleKey, false);
        doc.window_watchmatch_keys_bound = true;
        console.log("WatchMatch: Successfully bound keyboard shortcuts to", doc.title || "document");
    }

    let parentDoc = null;
    try {
        if (window.parent && window.parent.document) {
            parentDoc = window.parent.document;
        }
    } catch (e) {
        console.error("WatchMatch Error: Accessing parent document failed.", e);
    }
    const localDoc = document;

    [parentDoc, localDoc].forEach(bindShortcuts);

    // Try to hide hidden buttons on parent if accessible, otherwise local
    const targetDoc = parentDoc || localDoc;
    const hideButtons = () => {
        const buttons = Array.from(targetDoc.querySelectorAll('button'));
        const hiddenButtons = buttons.filter(b => b.innerText.includes('Hidden'));
        hiddenButtons.forEach(btn => {
            const btnContainer = btn.closest('div[data-testid="stButton"]');
            if (btnContainer) btnContainer.style.display = 'none';
        });
    };
    hideButtons();
    // Run again after a small delay in case they take a moment to render
    setTimeout(hideButtons, 100);
    setTimeout(hideButtons, 500);
    </script>
    """,
    height=1,
)



# ==========================================
# Sidebar Menu
# ==========================================
st.sidebar.title("🍿 WatchMatch Menu")

# Help & Instructions Expander
with st.sidebar.expander("ℹ️ Help & Instructions", expanded=False):
    st.markdown(
        """
        **Welcome to WatchMatch!**
        - **Step 1:** Enter your name and select your streaming services.
        - **Step 2:** Wait for your friends to join the same screen.
        - **Step 3:** Pick a genre and pick a mode.

        ### 🎬 Modes
        - **Movie Ranking:** Choose up to 5 movies from the 24-card list and assign unique ranks from 1 to 5 to any subset you like.
        - **Movie List:** Browse movies across the selected services and genre in a large 6x16 grid (96 movies at a time).

        ---
        
        *Keyboard Shortcuts (PC):*
        - **H**: Show help menu.
        - **L**: Open ranked list.
        - **R**: Open reset dialog.
        - **Space**: Trigger status refresh.
        """
    )

# Ranked Movie List Button
if st.sidebar.button("📊 Ranked Movie List", use_container_width=True, key="sidebar_ranked_list_btn"):
    show_movie_list_dialog()

# Platform Vibe theme selector
st.sidebar.selectbox(
    "🎨 Platform Vibe",
    ["Netflix (Red)", "HBO Max (Purple)", "Prime Video (Blue)", "Apple TV+ (Glass)"],
    key="sidebar_theme_style"
)

# Leave Session (only when logged in)
if st.session_state.get("user_name"):
    st.sidebar.divider()
    with st.sidebar.expander("🚪 Leave / Change Profile"):
        st.write("Leave the lobby to re-join with a different name or services. Other users are not affected.")
        if st.button("Leave Session", use_container_width=True, key="sidebar_leave_btn"):
            leave_lobby(st.session_state.user_name)
            st.rerun()

# Reset Session Expander
with st.sidebar.expander("⚠️ Reset Session"):
    st.write("This clears all active users, votes, and matches.")
    confirmation = st.text_input("Type 'reset' to confirm", key="sidebar_reset_confirmation", label_visibility="collapsed")
    if st.button("Reset Everything", type="primary", use_container_width=True, key="sidebar_reset_btn"):
        if confirmation.strip().lower() == "reset":
            reset_lobby()
            st.rerun()
        else:
            st.error("Type reset exactly to confirm.")

st.title("🍿 WatchMatch")
region_label = next((name for name, code in REGIONS.items() if code == lobby.get("region", "FI")), "your region")
st.markdown(f"Find the perfect movie for your group, available on your streaming services in {region_label}!")

refresh_col, _ = st.columns([1, 5])
with refresh_col:
    if st.button("Refresh", key="top_refresh_button"):
        st.rerun()

if "user_name" not in st.session_state:
    st.session_state.user_name = None

user_name = st.session_state.user_name


if not user_name:
    st.subheader("Join the Watch Party")

    # Region selector
    region_names = list(REGIONS.keys())
    current_region_code = lobby.get("region", "FI")
    current_region_name = next((k for k, v in REGIONS.items() if v == current_region_code), region_names[0])
    selected_region = st.selectbox("Region", region_names, index=region_names.index(current_region_name))
    lobby["region"] = REGIONS[selected_region]

    join_name = st.text_input("Your Name", key="join_name")
    
    current_providers = REGION_PROVIDERS.get(lobby["region"], REGION_PROVIDERS["FI"])
    join_services = st.multiselect(f"Your Streaming Services ({lobby['region']})", list(current_providers.keys()), key="join_services")

    if st.button("Join", type="primary"):
        join_name = join_name.strip()
        if join_name:
            lobby["users"][join_name] = {
                "services": join_services,
                "votes": {},
                "round2_votes": {},
                "ready": False,
                "index": 0,
            }
            st.session_state.user_name = join_name
            st.rerun()
        else:
            st.error("Please enter your name to join.")
else:
    if lobby["match"]:
        st.success("🎉 IT'S A MATCH! 🎉")
        st.markdown(f"### You are all watching: **{lobby['match']['title']}** tonight!")
        if lobby["match"].get("poster_path"):
            st.image(f"{TMDB_IMAGE_BASE}{lobby['match']['poster_path']}", width=300)
        st.markdown(lobby["match"].get("overview", ""))
        
        # Display all available streaming services
        providers = fetch_movie_watch_providers(lobby["match"]["id"], lobby.get("region", "FI"))
        if providers:
            shared_services = get_combined_service_names()
            if shared_services:
                matched = [p for p in providers if p in shared_services]
                if matched:
                    st.markdown(f"📺 **Available on your services:** {', '.join(matched)}")
                else:
                    st.markdown("📺 **Available on your services:** None (N/A)")
            else:
                # No services selected, show all
                st.markdown(f"📺 **Available on:** {', '.join(providers)}")
        else:
            st.markdown("📺 **Available on:** Not found or not streaming in Finland.")

        if st.button("Start Over / New Search"):
            reset_lobby()
            st.rerun()
        st.stop()

    if lobby["state"] == "SETUP":
        st.subheader("Watch Party Setup")
        st.markdown("Waiting for everyone to join...")
        auto_refresh_page()

        st.markdown("### Participants")
        combined_services = set()
        for name, data in lobby["users"].items():
            st.write(f"- **{name}**: {', '.join(data['services']) if data['services'] else 'No services'}")
            combined_services.update(data["services"])

        st.markdown(f"**Combined Services:** {', '.join(combined_services) if combined_services else 'None'}")

        if st.button("Refresh Participants"):
            st.rerun()

        st.markdown("---")
        st.markdown("### Select Region, Genre & Start")

        genre_keys = list(GENRES.keys())
        genre_name = st.selectbox("Genre", genre_keys)

        if st.button("Movie Ranking", type="primary", key="setup_movie_ranking_btn"):
            if not start_matching("rating", genre_name):
                st.error("No movies found for this combination of genre and streaming services.")
        st.caption("Rank 24 movies with 1-5 used only once each.")

        if st.button("Movie List", type="secondary", key="setup_movie_list_btn"):
            if not start_browsing(genre_name):
                st.error("No movies found for this combination of genre and streaming services.")
        st.caption("Browse a 6x16 grid of movies across your selected services (single-user quick discovery).")

        st.markdown("---")
        st.markdown("**Or browse the most popular movies across all genres:**")
        any_col_a, any_col_b = st.columns(2)
        with any_col_a:
            if st.button("🎬 Any Genre — Rank & Vote", use_container_width=True):
                if not start_matching("rating", None):
                    st.error("No movies found for your streaming services.")
        with any_col_b:
            if st.button("🎬 Any Genre — Movie List", use_container_width=True):
                if not start_browsing(None):
                    st.error("No movies found for your streaming services.")

    elif lobby["state"] == "RATING":
        st.subheader(f"Genre: {lobby['genre'] or 'Any Genre'} - Phase 1: Movie Ranking")
        st.markdown("Choose up to 5 movies from the 24-card list and assign unique ranks from 1 to 5 to any subset you like. Then click 'Submit Ranking' at the bottom.")

        user_data = lobby["users"][user_name]
        movies_to_show = current_movie_batch()
        shared_service_names = get_combined_service_names()
        show_service_names = len(shared_service_names) > 1

        if not movies_to_show:
            st.info("No movies found.")
            if st.button("Go Back to Setup"):
                lobby["state"] = "SETUP"
                st.rerun()
            st.stop()

        st.caption("These 24 movies are sorted by TMDB popularity, highest first.")

        cols_per_row = 6
        for i in range(0, len(movies_to_show), cols_per_row):
            cols = st.columns(cols_per_row)
            for j in range(cols_per_row):
                if i + j < len(movies_to_show):
                    movie = movies_to_show[i + j]
                    with cols[j]:
                        st.markdown('<div class="movie-card-marker"></div>', unsafe_allow_html=True)
                        rank_number = i + j + 1
                        st.caption(
                            f"#{rank_number} · popularity {_safe_float(movie.get('popularity')):.1f} · votes {int(_safe_float(movie.get('vote_count')))}"
                        )
                        if show_service_names:
                            available_services = [
                                service_name
                                for service_name in fetch_movie_watch_providers(movie["id"], lobby.get("region", "FI"))
                                if service_name in shared_service_names
                            ]
                            if available_services:
                                st.caption(f"Available on: {', '.join(available_services)}")
                        if movie.get("poster_path"):
                            st.image(f"{TMDB_IMAGE_BASE}{movie['poster_path']}", use_container_width=True)
                        else:
                            st.write("No poster available")

                        current_vote = user_data["votes"].get(movie["id"], 0)
                        options = [1, 2, 3, 4, 5]
                        default_val = current_vote if current_vote > 0 else None

                        st.segmented_control(
                            "Rank",
                            options,
                            selection_mode="single",
                            default=default_val,
                            format_func=str,
                            key=f"rate_{movie['id']}",
                            label_visibility="collapsed",
                            on_change=apply_rank_selection,
                            args=(movie["id"], user_name),
                        )
                        with st.expander("ℹ️ Info"): 
                            st.write(f"**{movie['title']}** ({movie.get('release_date', '')[:4]})")
                            st.caption(f"Rating: {movie.get('vote_average', 'N/A')}")
                            st.write(movie.get("overview", "No overview."))

        st.markdown("---")
        if user_data["ready"]:
            st.success("Waiting for other friends to finish ranking...")
            auto_refresh_page()
            if st.button("Refresh Status"):
                st.rerun()
        else:
            if st.button("Submit Ranking", type="primary"):
                submitted_votes = [user_data["votes"].get(movie["id"], 0) for movie in movies_to_show]
                ranked_votes = [vote for vote in submitted_votes if vote > 0]
                if not ranked_votes:
                    st.error("Rank between 1 and 5 movies before submitting.")
                elif len(ranked_votes) > 5:
                    st.error("You can only rank up to 5 movies.")
                elif len(set(ranked_votes)) != len(ranked_votes):
                    st.error("Use each rank from 1 to 5 at most once.")
                else:
                    lobby["users"][user_name]["ready"] = True

                    all_ready = all(u["ready"] for u in lobby["users"].values())
                    if all_ready:
                        if movie_batch_has_votes():
                            lobby["state"] = "ROUND_2"
                        else:
                            load_next_movie_batch()
                    st.rerun()

    elif lobby["state"] == "ROUND_2":
        st.subheader("Phase 2: Final Vote")
        st.markdown("Here are the top ranked movies. Vote **Yes** to any movie you'd watch. If everyone votes Yes, it's a Match!")

        movie_scores = []
        for movie in lobby["movies"]:
            total_score = sum(u["votes"].get(movie["id"], 0) for u in lobby["users"].values())
            if total_score > 0:
                movie_scores.append((total_score, movie))

        movie_scores.sort(key=lambda item: item[0], reverse=True)
        top_movies = [movie for _, movie in movie_scores[:5]]

        user_data = lobby["users"][user_name]

        if not top_movies:
            st.warning("Nobody liked any movies! Ready for a new batch?")
            col_a, col_b = st.columns(2)
            with col_a:
                if st.button("Load New Movies", type="primary"):
                    load_next_movie_batch()
            with col_b:
                if st.button("Start Over (Setup)"):
                    reset_lobby()
                    st.rerun()
            st.stop()

        if st.button("Refresh Status"):
            st.rerun()

        cols_per_row = 6
        for i in range(0, len(top_movies), cols_per_row):
            cols = st.columns(cols_per_row)
            for j in range(cols_per_row):
                if i + j < len(top_movies):
                    movie = top_movies[i + j]
                    with cols[j]:
                        st.markdown('<div class="movie-card-marker"></div>', unsafe_allow_html=True)
                        if movie.get("poster_path"):
                            st.image(f"{TMDB_IMAGE_BASE}{movie['poster_path']}", use_container_width=True)
                        else:
                            st.write("No poster available")

                        is_yes = user_data["round2_votes"].get(movie["id"], False)
                        new_yes = st.checkbox("Vote Yes", value=is_yes, key=f"yes_{movie['id']}")

                        if new_yes != is_yes:
                            lobby["users"][user_name]["round2_votes"][movie["id"]] = new_yes
                            if new_yes:
                                all_yes = all(u["round2_votes"].get(movie["id"], False) for u in lobby["users"].values())
                                if all_yes:
                                    lobby["match"] = movie
                                    st.rerun()
                        with st.expander("ℹ️ Info"): 
                            st.write(f"**Total Popcorns:** {sum(u['votes'].get(movie['id'], 0) for u in lobby['users'].values())} 🍿")
                            st.write(f"**{movie['title']}** ({movie.get('release_date', '')[:4]})")
                            st.write(movie.get("overview", "No overview."))

        st.markdown("---")
        st.markdown("Not feeling any of these?")
        col_a, col_b = st.columns(2)
        with col_a:
            if st.button("Load New Movies", type="primary"):
                load_next_movie_batch()
        with col_b:
            if st.button("Start Over (Setup)"):
                reset_lobby()
                st.rerun()

    elif lobby["state"] == "BROWSE":
        region_code = lobby.get("region", "FI")
        region_label = next((name for name, code in REGIONS.items() if code == region_code), "your region")
        st.subheader(f"Genre: {lobby['genre'] or 'Any Genre'} - Browse Mode")
        st.markdown(f"Quickly browse popular movies across your selected services in {region_label}.")

        cursor = lobby.get("movie_cursor", 0)
        movies_to_show = lobby.get("movie_pool", [])[cursor : cursor + 96]

        if not movies_to_show:
            st.info("No movies found.")
            if st.button("Go Back to Setup"):
                lobby["state"] = "SETUP"
                st.rerun()
            st.stop()

        cols_per_row = 6
        shared_service_names = get_combined_service_names()
        show_service_names = len(shared_service_names) > 0

        for i in range(0, len(movies_to_show), cols_per_row):
            cols = st.columns(cols_per_row)
            for j in range(cols_per_row):
                if i + j < len(movies_to_show):
                    movie = movies_to_show[i + j]
                    with cols[j]:
                        st.markdown('<div class="movie-card-marker"></div>', unsafe_allow_html=True)
                        rank_num = cursor + i + j + 1
                        st.caption(f"#{rank_num}")

                        if show_service_names:
                            available_services = [
                                service_name
                                for service_name in fetch_movie_watch_providers(movie["id"], region_code)
                                if service_name in shared_service_names
                            ]
                            if available_services:
                                st.caption(f"📺 {', '.join(available_services)}")
                            else:
                                st.caption("📺 N/A")

                        if movie.get("poster_path"):
                            st.image(f"{TMDB_IMAGE_BASE}{movie['poster_path']}", use_container_width=True)
                        else:
                            st.write("No poster available")

                        st.markdown(f"**{movie.get('title', 'Untitled')}**")
                        year = (movie.get("release_date", "") or "")[:4]
                        rating = movie.get("vote_average", "N/A")
                        st.caption(f"📅 {year}  ·  ⭐ {rating}")

                        with st.expander("ℹ️ Details"):
                            st.write(movie.get("overview", "No overview available."))

        st.markdown("---")
        nav_col1, nav_col2, nav_col3 = st.columns([1, 2, 1])
        with nav_col1:
            if cursor > 0:
                if st.button("⬅️ Previous Page", use_container_width=True):
                    load_prev_browse_page()
        with nav_col2:
            if st.button("⚙️ Back to Setup", use_container_width=True):
                lobby["state"] = "SETUP"
                st.rerun()
        with nav_col3:
            if st.button("Next Page ➡️", use_container_width=True):
                load_next_browse_page()
