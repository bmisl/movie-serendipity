import random
import string
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
MATCH_BATCH_SIZE = 24
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
    "Action": 28,
    "Adventure": 12,
    "Animation": 16,
    "Comedy": 35,
    "Crime": 80,
    "Drama": 18,
    "Fantasy": 14,
    "Horror": 27,
    "Romance": 10749,
    "Sci-Fi": 878,
    "Thriller": 53,
}

REGIONS = {
    "Finland 🇫🇮": "FI",
    "Denmark 🇩🇰": "DK",
    "Iceland 🇮🇸": "IS",
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
        "swipe_votes": {},
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


def build_discover_params(genre_id: int, provider_ids: List[int], page: int, region: str = "FI") -> dict:
    params = {
        "api_key": TMDB_API_KEY,
        "language": "en-US",
        "watch_region": region,
        "region": region, # Restricts release dates to the selected region
        "with_genres": genre_id,
        "sort_by": "popularity.desc",
        "page": page,
        "with_original_language": "en|fi|da|sv|no|is",
        "vote_count.gte": 100,
    }
    if provider_ids:
        params["with_watch_providers"] = "|".join(map(str, provider_ids))
    return params


@st.cache_data(show_spinner=False, ttl=3600)
def fetch_ranked_movies(genre_id: int, provider_ids: tuple[int, ...], limit: int, region: str = "FI") -> List[dict]:
    movies: List[dict] = []
    page = 1
    total_pages: Optional[int] = None

    while len(movies) < limit and (total_pages is None or page <= total_pages):
        res = requests.get(f"{TMDB_BASE_URL}/discover/movie", params=build_discover_params(genre_id, list(provider_ids), page, region))
        if res.status_code != 200:
            break
        payload = res.json()
        movies.extend(payload.get("results", []))
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
    return tuple(providers_map[service] for service in combined_services if service in providers_map)


def get_combined_service_names() -> List[str]:
    combined_services = set()
    for user in lobby["users"].values():
        combined_services.update(user["services"])
    return sorted(combined_services)


def get_movie_pool(genre_name: str, limit: int = MATCH_POOL_SIZE) -> List[dict]:
    if not genre_name:
        return []
    return fetch_ranked_movies(GENRES[genre_name], get_combined_provider_ids(), limit, lobby.get("region", "FI"))


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
            "swipe_votes": {},
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


def initialise_swipe_cursors() -> None:
    for user in lobby["users"].values():
        user["swipe_cursor"] = 0
        user["swipe_done"] = False


def load_next_movie_batch() -> None:
    next_cursor = lobby.get("movie_cursor", 0) + MATCH_BATCH_SIZE
    pool = lobby.get("movie_pool", [])
    if next_cursor + MATCH_BATCH_SIZE > len(pool):
        pool = get_movie_pool(lobby["genre"], max(MATCH_POOL_SIZE, next_cursor + MATCH_BATCH_SIZE))
        lobby["movie_pool"] = pool
    lobby["movie_cursor"] = next_cursor
    lobby["movies"] = pool[next_cursor: next_cursor + MATCH_BATCH_SIZE]
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


def start_matching(mode: str, genre_name: str) -> bool:
    lobby["genre"] = genre_name
    lobby["movie_cursor"] = 0
    lobby["movie_pool"] = get_movie_pool(genre_name, MATCH_POOL_SIZE)
    lobby["mode"] = mode
    lobby["swipe_votes"] = {}
    if mode == "swipe":
        initialise_swipe_cursors()
        lobby["movies"] = lobby["movie_pool"][:1]
        lobby["state"] = "SWIPE"
    else:
        lobby["movies"] = lobby["movie_pool"][:MATCH_BATCH_SIZE]
        lobby["state"] = "RATING"
    if lobby["movies"]:
        st.rerun()
        return True
    return False


def current_swipe_movie() -> Optional[dict]:
    pool = lobby.get("movie_pool", [])
    user = lobby["users"].get(st.session_state.user_name or "")
    cursor = int((user or {}).get("swipe_cursor", 0) or 0)
    if 0 <= cursor < len(pool):
        return pool[cursor]
    return None


def advance_user_swipe_movie(user_name: str) -> None:
    user = lobby["users"].get(user_name)
    if not user:
        return
    user["swipe_cursor"] = int(user.get("swipe_cursor", 0) or 0) + 1
    pool = lobby.get("movie_pool", [])
    user["swipe_done"] = user["swipe_cursor"] >= len(pool)
    lobby["state"] = "SWIPE"
    st.rerun()


def record_swipe_vote(movie_id: int, user: str, liked: bool) -> None:
    votes_for_movie = lobby.setdefault("swipe_votes", {}).setdefault(movie_id, {})
    if votes_for_movie.get(user) == liked:
        return
    votes_for_movie[user] = liked

    if all(votes_for_movie.get(name, False) for name in lobby["users"]):
        lobby["match"] = next((movie for movie in lobby["movie_pool"] if movie["id"] == movie_id), None)
        st.rerun()
        return

    advance_user_swipe_movie(user)


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
    batch = pool[cursor: cursor + MATCH_BATCH_SIZE]
    if batch:
        return batch
    return sort_movies_by_popularity(lobby.get("movies", []))


@st.cache_data(show_spinner=False, ttl=3600)
def fetch_movie_watch_providers(movie_id: int, region: str = "FI") -> List[str]:
    try:
        response = requests.get(f"{TMDB_BASE_URL}/movie/{movie_id}/watch/providers", params={"api_key": TMDB_API_KEY})
        if response.status_code != 200:
            return []
        payload = response.json().get("results", {}).get(region, {})
        provider_names: List[str] = []
        for bucket in ("flatrate", "ads", "buy", "rent"):
            for provider in payload.get(bucket, []) or []:
                name = provider.get("provider_name")
                if name and name not in provider_names:
                    provider_names.append(name)
        return provider_names
    except Exception:
        return []


st.set_page_config(page_title="WatchMatch", layout="wide", initial_sidebar_state="collapsed")


@st.dialog("Help & Instructions")
def show_help_dialog():
    st.markdown(
        """
    **Welcome to WatchMatch!**
    - **Step 1:** Enter your name and select your streaming services.
    - **Step 2:** Wait for your friends to join the same screen.
    - **Step 3:** Pick a Genre and select a matching mode to start:

    ### 🎬 Matching Modes
    
    #### 1. 24-Movie Ranking (Rank & Vote)
    - **Phase 1 (Rating):** Rate 24 popular movies from 1 to 5 stars.
    - **Phase 2 (Final Vote):** The top-rated movies are shown. Vote **Yes** to any movie you'd watch. If everyone votes Yes, it's a match!
    
    #### 2. Swipe Match (Fast Swiping)
    - Movies are shown one-by-one. Click **Like** if you want to watch it, or **Skip** if you don't.
    - If all participants like the same movie, it is a match!

    ---
    
    *Keyboard Shortcuts:*
    - **H**: Show this help menu.
    - **L**: Open the ranked movie list for the active genre.
    - **R**: Open the reset dialog.
    - **D**: Toggle Dark Mode.
    - **Spacebar**: Trigger a manual status refresh.
    - **Left Arrow**: **Skip** the current movie (during Swipe Match).
    - **Right Arrow**: **Like** the current movie (during Swipe Match).
    """
    )


if st.button("HiddenHelp"):
    show_help_dialog()


@st.dialog("Ranked Movie List", width="large")
def show_movie_list_dialog():
    if not lobby["genre"]:
        st.info("Choose a genre first.")
        return

    region = lobby.get("region", "FI")
    provider_ids = get_combined_provider_ids()
    sort_by = st.radio("Sort by", ["📈 Popularity", "⭐ TMDB Rating"], horizontal=True)

    with st.spinner("Fetching ranked movies and checking streaming services..."):
        try:
            ranked_movies = fetch_ranked_movies(GENRES[lobby["genre"]], provider_ids, LIST_BATCH_SIZE, region)
        except Exception:
            ranked_movies = []

        if not ranked_movies:
            st.info("No movies found for this genre and the selected streaming services.")
            return

        if "TMDB Rating" in sort_by:
            ranked_movies = sorted(ranked_movies, key=lambda m: _safe_float(m.get("vote_average"), -1), reverse=True)

        shared_services = get_combined_service_names()
        rows = []
        for rank, movie in enumerate(ranked_movies, start=1):
            # Resolve one available service for the movie
            providers = fetch_movie_watch_providers(movie["id"], region)
            service = next((p for p in providers if p in shared_services), None)
            if not service and providers:
                service = providers[0]
            if not service:
                service = "N/A"

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
    st.caption(f"Showing the top {min(LIST_BATCH_SIZE, len(ranked_movies))} movies sorted by {sort_label} for the selected genre and shared streaming services.")
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
            
            // Robust check for left and right arrows
            const isLeft = (key === 'arrowleft' || key === 'left' || e.code === 'ArrowLeft' || e.keyCode === 37);
            const isRight = (key === 'arrowright' || key === 'right' || e.code === 'ArrowRight' || e.keyCode === 39);

            console.log("WatchMatch keydown:", { key, code: e.code, keyCode: e.keyCode, isLeft, isRight, targetTag });

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
            } else if (isLeft) {
                const skipBtn = buttons.find(b => (b.innerText || '').toLowerCase().includes('skip'));
                if (skipBtn) {
                    console.log("WatchMatch: Clicking Skip button");
                    skipBtn.click();
                    handled = true;
                } else {
                    console.log("WatchMatch: Skip button not found in active buttons:", buttons.map(b => b.innerText));
                }
            } else if (isRight) {
                const likeBtn = buttons.find(b => (b.innerText || '').toLowerCase().includes('like'));
                if (likeBtn) {
                    console.log("WatchMatch: Clicking Like button");
                    likeBtn.click();
                    handled = true;
                } else {
                    console.log("WatchMatch: Like button not found in active buttons:", buttons.map(b => b.innerText));
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

st.markdown(
    """
<style>
html.custom-dark-mode {
    filter: invert(1) hue-rotate(180deg);
}
html.custom-dark-mode img, html.custom-dark-mode video, html.custom-dark-mode iframe {
    filter: invert(1) hue-rotate(180deg);
}
</style>
""",
    unsafe_allow_html=True,
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
        - **Step 3:** Pick a Genre and select a matching mode to start:

        ### 🎬 Matching Modes
        
        #### 1. 24-Movie Ranking (Rank & Vote)
        - **Phase 1 (Rating):** Rate 24 popular movies from 1 to 5 stars.
        - **Phase 2 (Final Vote):** Vote **Yes** to any movie you'd watch. If everyone votes Yes, it's a match!
        
        #### 2. Swipe Match (Fast Swiping)
        - Movies are shown one-by-one. Click **Like** (or press **Right Arrow**) or **Skip** (or press **Left Arrow**).
        - If all participants like the same movie, it is a match!

        ---
        
        *Keyboard Shortcuts (PC):*
        - **H**: Show help menu.
        - **L**: Open ranked list.
        - **R**: Open reset dialog.
        - **D**: Toggle Dark Mode.
        - **Space**: Trigger status refresh.
        - **Left Arrow**: **Skip** movie.
        - **Right Arrow**: **Like** movie.
        """
    )

# Ranked Movie List Button
if lobby["genre"]:
    if st.sidebar.button("📊 Ranked Movie List", use_container_width=True, key="sidebar_ranked_list_btn"):
        show_movie_list_dialog()
else:
    st.sidebar.info("Choose a genre to view the Ranked Movie List.")

# Dark Mode Toggle (Default to True)
if "dark_mode" not in st.session_state:
    st.session_state.dark_mode = True

dark_mode = st.sidebar.toggle("🌙 Dark Mode", value=st.session_state.dark_mode, key="sidebar_dark_mode")

# Apply dark mode globally if active
if dark_mode:
    st.markdown(
        """
        <style>
        html {
            filter: invert(1) hue-rotate(180deg);
        }
        html img, html video, html iframe {
            filter: invert(1) hue-rotate(180deg);
        }
        </style>
        """,
        unsafe_allow_html=True,
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
st.markdown("Find the perfect movie for your group, available on your streaming services in Finland!")

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
        if join_name:
            lobby["users"][join_name] = {
                "services": join_services,
                "votes": {},
                "round2_votes": {},
                "ready": False,
                "index": 0,
                "swipe_cursor": 0,
                "swipe_done": False,
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
            matched = [p for p in providers if p in shared_services]
            if matched:
                st.markdown(f"📺 **Available on your services:** {', '.join(matched)}")
                other = [p for p in providers if p not in shared_services]
                if other:
                    st.caption(f"Also streaming on: {', '.join(other)}")
            else:
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

        # Genre selector with Surprise Me
        genre_keys = list(GENRES.keys())
        if "selected_genre" not in st.session_state:
            st.session_state.selected_genre = genre_keys[0]
        genre_col, surprise_col = st.columns([4, 1])
        with surprise_col:
            if st.button("🎲 Surprise!", use_container_width=True, help="Pick a random genre"):
                st.session_state.selected_genre = random.choice(genre_keys)
                st.rerun()
        with genre_col:
            genre_name = st.selectbox(
                "Genre", genre_keys,
                index=genre_keys.index(st.session_state.selected_genre)
                      if st.session_state.selected_genre in genre_keys else 0
            )
        st.session_state.selected_genre = genre_name

        st.markdown("Choose how you want to match.")
        mode_col_a, mode_col_b = st.columns(2)
        with mode_col_a:
            if st.button("24-Movie Ranking", type="primary"):
                if not start_matching("rating", genre_name):
                    st.error("No movies found for this combination of genre and streaming services.")
            st.caption("24 movies at a time with rating phases.")
        with mode_col_b:
            if st.button("Swipe Match", type="primary"):
                if not start_matching("swipe", genre_name):
                    st.error("No movies found for this combination of genre and streaming services.")
            st.caption("Swipe movies one by one until you match.")

    elif lobby["state"] == "SWIPE":
        st.subheader(f"Genre: {lobby['genre']} - Swipe Match")
        st.markdown("Swipe through movies one at a time. If everyone likes the same movie, it's a match.")

        current_movie = current_swipe_movie()
        if not current_movie:
            st.markdown("### 🎬 You've seen them all!")
            st.info(
                "You've swiped through all available movies without a group match. "
                "Try loading more movies, picking a different genre, or just flip a coin!"
            )
            col_a, col_b = st.columns(2)
            with col_a:
                if st.button("🔄 Load More Movies", type="primary", use_container_width=True):
                    # Extend the pool and reset swipe cursors
                    current_pool_size = len(lobby.get("movie_pool", []))
                    lobby["movie_pool"] = get_movie_pool(lobby["genre"], current_pool_size + MATCH_POOL_SIZE)
                    initialise_swipe_cursors()
                    st.rerun()
            with col_b:
                if st.button("⚙️ Back to Setup", use_container_width=True):
                    lobby["state"] = "SETUP"
                    st.rerun()
            st.stop()

        shared_service_names = get_combined_service_names()
        show_service_names = len(shared_service_names) > 1
        user_votes = lobby.setdefault("swipe_votes", {}).setdefault(current_movie["id"], {})

        user_swipe_pos = int((lobby["users"].get(user_name) or {}).get("swipe_cursor", 0) or 0) + 1
        st.caption(f"Movie {user_swipe_pos}")
        st.caption(
            f"Popularity {_safe_float(current_movie.get('popularity')):.1f} · votes {int(_safe_float(current_movie.get('vote_count')))}"
        )
        if show_service_names:
            available_services = [
                service_name
                for service_name in fetch_movie_watch_providers(current_movie["id"], lobby.get("region", "FI"))
                if service_name in shared_service_names
            ]
            if available_services:
                st.caption(f"Available on: {', '.join(available_services)}")

        current_vote = user_votes.get(user_name)
        
        # Center the movie card using a [1, 2, 1] column layout
        swipe_col_left, swipe_col_center, swipe_col_right = st.columns([1, 2, 1])
        with swipe_col_center:
            if current_movie.get("poster_path"):
                st.markdown(
                    f'<div style="display: flex; justify-content: center; margin-bottom: 15px;">'
                    f'<img src="{TMDB_IMAGE_BASE}{current_movie["poster_path"]}" style="max-height: 280px; border-radius: 12px; box-shadow: 0 4px 15px rgba(0,0,0,0.15);">'
                    f'</div>',
                    unsafe_allow_html=True,
                )
            st.markdown(f"<h3 style='text-align: center; margin-top: 0px;'>{current_movie['title']}</h3>", unsafe_allow_html=True)
            st.write(current_movie.get("overview", "No overview."))
            
            if current_vote is None:
                # Add spacing
                st.write("")
                # Put Skip and Like side-by-side
                btn_col1, btn_col2 = st.columns(2)
                with btn_col1:
                    if st.button("Skip", use_container_width=True):
                        record_swipe_vote(current_movie["id"], user_name, False)
                with btn_col2:
                    if st.button("Like", type="primary", use_container_width=True):
                        record_swipe_vote(current_movie["id"], user_name, True)
            else:
                waiting_count = len(user_votes)
                if waiting_count < len(lobby["users"]):
                    st.success("Waiting for the other users to swipe this movie...")
                    auto_refresh_page()
                    if st.button("Refresh Status"):
                        st.rerun()
                elif all(user_votes.get(name, False) for name in lobby["users"]):
                    st.success("It’s a match!")
                    if st.button("Show Match"):
                        st.rerun()
                else:
                    st.info("No match on this movie. Moving to the next one...")
                    auto_refresh_page(1500)
                    if st.button("Next Movie Now"):
                        advance_user_swipe_movie(user_name)

    elif lobby["state"] == "RATING":
        st.subheader(f"Genre: {lobby['genre']} - Phase 1: Rating")
        st.markdown("Rate the movies from 0 to 5. When you are done, click 'Submit Ratings' at the bottom.")

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

        st.caption("Movies are sorted by TMDB popularity, highest first.")

        cols_per_row = 6
        for i in range(0, len(movies_to_show), cols_per_row):
            cols = st.columns(cols_per_row)
            for j in range(cols_per_row):
                if i + j < len(movies_to_show):
                    movie = movies_to_show[i + j]
                    with cols[j]:
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

                        def format_popcorns(value):
                            return str(value)

                        default_val = current_vote if current_vote > 0 else None

                        new_vote = st.segmented_control(
                            "Rating",
                            options,
                            selection_mode="single",
                            default=default_val,
                            format_func=format_popcorns,
                            key=f"rate_{movie['id']}",
                            label_visibility="collapsed",
                        )

                        final_vote = new_vote if new_vote is not None else 0

                        if final_vote != current_vote:
                            lobby["users"][user_name]["votes"][movie["id"]] = final_vote
                            st.rerun()

                        with st.expander("ℹ️ Info"):
                            st.write(f"**{movie['title']}** ({movie.get('release_date', '')[:4]})")
                            st.caption(f"⭐ {movie.get('vote_average', 'N/A')}")
                            st.write(movie.get("overview", "No overview."))

        st.markdown("---")
        if user_data["ready"]:
            st.success("Waiting for other friends to finish rating...")
            auto_refresh_page()
            if st.button("Refresh Status"):
                st.rerun()
        else:
            if st.button("Submit Ratings", type="primary"):
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
        st.markdown("Here are the top rated movies. Vote **Yes** to any movie you'd watch. If everyone votes Yes, it's a Match!")

        movie_scores = []
        for movie in lobby["movies"]:
            total_score = sum(u["votes"].get(movie["id"], 0) for u in lobby["users"].values())
            if total_score > 0:
                movie_scores.append((total_score, movie))

        movie_scores.sort(key=lambda item: item[0], reverse=True)
        top_movies = [movie for _, movie in movie_scores[:6]]

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
