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

# Hardcoded FI providers for prototype
PROVIDERS = {
    "Netflix": 8,
    "Amazon Prime Video": 119,
    "Disney Plus": 337,
    "HBO Max": 1899,
    "Viaplay": 76,
    "Ruutu": 338,
    "Yle Areena": 323,
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


@st.cache_resource
def get_global_session() -> dict:
    return {
        "users": {},
        "genre": None,
        "movies": [],
        "movie_pool": [],
        "movie_cursor": 0,
        "state": "SETUP",
        "match": None,
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


def build_discover_params(genre_id: int, provider_ids: List[int], page: int) -> dict:
    params = {
        "api_key": TMDB_API_KEY,
        "language": "en-US",
        "watch_region": "FI",
        "with_genres": genre_id,
        "sort_by": "popularity.desc",
        "page": page,
    }
    if provider_ids:
        params["with_watch_providers"] = "|".join(map(str, provider_ids))
    return params


@st.cache_data(show_spinner=False, ttl=3600)
def fetch_ranked_movies(genre_id: int, provider_ids: tuple[int, ...], limit: int) -> List[dict]:
    movies: List[dict] = []
    page = 1
    total_pages: Optional[int] = None

    while len(movies) < limit and (total_pages is None or page <= total_pages):
        res = requests.get(f"{TMDB_BASE_URL}/discover/movie", params=build_discover_params(genre_id, list(provider_ids), page))
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
    return tuple(PROVIDERS[service] for service in combined_services)


def get_combined_service_names() -> List[str]:
    combined_services = set()
    for user in lobby["users"].values():
        combined_services.update(user["services"])
    return sorted(combined_services)


def get_movie_pool(genre_name: str, limit: int = MATCH_POOL_SIZE) -> List[dict]:
    if not genre_name:
        return []
    return fetch_ranked_movies(GENRES[genre_name], get_combined_provider_ids(), limit)


def reset_lobby() -> None:
    lobby.clear()
    lobby.update(
        {
            "users": {},
            "genre": None,
            "movies": [],
            "movie_pool": [],
            "movie_cursor": 0,
            "state": "SETUP",
            "match": None,
        }
    )
    st.session_state.user_name = None
    for key in ("join_name", "join_services", "reset_confirmation"):
        if key in st.session_state:
            del st.session_state[key]


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
def fetch_movie_watch_providers(movie_id: int) -> List[str]:
    try:
        response = requests.get(f"{TMDB_BASE_URL}/movie/{movie_id}/watch/providers", params={"api_key": TMDB_API_KEY})
        if response.status_code != 200:
            return []
        payload = response.json().get("results", {}).get("FI", {})
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
    - **Step 3:** Pick a Genre and click 'Start Matching!'.
    - **Phase 1:** Rate the most popular movies first. Lower-ranked titles appear if nobody picks from the current batch.
    - **Phase 2:** The top movies are shown. Vote 'Yes' to finalize your choice.
    - **No Matches?** If no one can agree on a movie from the 1st round, you can repeat the process for a 2nd and then a 3rd round with new movies. If that doesn't work, then it is no longer a movie night - instead, you go out for a walk!

    *Shortcuts:*
    - Press **H** to show this help menu.
    - Press **L** to open the ranked movie list for the active genre.
    - Press **R** to open the reset dialog. Type `reset` to confirm.
    - Press **D** to toggle Dark Mode.
    """
    )


if st.button("HiddenHelp"):
    show_help_dialog()


@st.dialog("Ranked Movie List")
def show_movie_list_dialog():
    if not lobby["genre"]:
        st.info("Choose a genre first.")
        return

    provider_ids = get_combined_provider_ids()
    try:
        ranked_movies = fetch_ranked_movies(GENRES[lobby["genre"]], provider_ids, LIST_BATCH_SIZE)
    except Exception:
        ranked_movies = []

    if not ranked_movies:
        st.info("No movies found for this genre and the selected streaming services.")
        return

    st.caption(f"Showing the top {min(LIST_BATCH_SIZE, len(ranked_movies))} movies by TMDB popularity for the selected genre and shared streaming services.")
    rows = []
    for rank, movie in enumerate(ranked_movies, start=1):
        rows.append(
            {
                "Rank": rank,
                "Title": movie.get("title", "Untitled"),
                "Popularity": round(_safe_float(movie.get("popularity")), 2),
                "TMDB Rating": movie.get("vote_average", "N/A"),
                "Year": (movie.get("release_date", "") or "")[:4],
            }
        )
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
        if (!doc || doc.window_watchmatch_keys_bound) {
            return;
        }

        const handleKey = function(e) {
            const targetTag = (e.target && e.target.tagName) ? e.target.tagName : "";
            if (targetTag === 'INPUT' || targetTag === 'TEXTAREA') return;

            const key = (e.key || '').toLowerCase();
            const buttons = Array.from(doc.querySelectorAll('button'));
            let handled = false;

            if (key === 'h') {
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
                doc.documentElement.classList.toggle('custom-dark-mode');
                handled = true;
            }

            if (handled) {
                e.preventDefault();
                e.stopPropagation();
            }
        };

        doc.addEventListener('keydown', handleKey, false);
        doc.window_watchmatch_keys_bound = true;
    }

    const parentDoc = window.parent && window.parent.document ? window.parent.document : null;
    const localDoc = document;

    [parentDoc, localDoc].forEach(bindShortcuts);

    const buttons = Array.from((parentDoc || localDoc).querySelectorAll('button'));
    const hiddenButtons = buttons.filter(b => b.innerText.includes('Hidden'));
    hiddenButtons.forEach(btn => {
        const btnContainer = btn.closest('div[data-testid="stButton"]');
        if (btnContainer) btnContainer.style.display = 'none';
    });
    </script>
    """,
    height=0,
    width=0,
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

st.title("🍿 WatchMatch")
st.markdown("Find the perfect movie for your group, available on your streaming services in Finland!")

if "user_name" not in st.session_state:
    st.session_state.user_name = None

user_name = st.session_state.user_name


if not user_name:
    st.subheader("Join the Watch Party")

    join_name = st.text_input("Your Name", key="join_name")
    join_services = st.multiselect("Your Streaming Services (FI)", list(PROVIDERS.keys()), key="join_services")

    if st.button("Join", type="primary"):
        if join_name:
            lobby["users"][join_name] = {"services": join_services, "votes": {}, "round2_votes": {}, "ready": False, "index": 0}
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
        st.markdown("### Select Genre & Start")
        genre_name = st.selectbox("Genre", list(GENRES.keys()))

        if st.button("Start Matching!"):
            lobby["genre"] = genre_name
            lobby["movie_cursor"] = 0
            lobby["movie_pool"] = get_movie_pool(genre_name, MATCH_POOL_SIZE)
            movies = lobby["movie_pool"][:MATCH_BATCH_SIZE]
            if movies:
                lobby["movies"] = movies
                lobby["state"] = "RATING"
                st.rerun()
            else:
                st.error("No movies found for this combination of genre and streaming services.")

    elif lobby["state"] == "RATING":
        st.subheader(f"Genre: {lobby['genre']} - Phase 1: Rating")
        if st.button("Ranked Movie List"):
            show_movie_list_dialog()
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
                                for service_name in fetch_movie_watch_providers(movie["id"])
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
                if all_ready and len(lobby["users"]) > 1:
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
                                if all_yes and len(lobby["users"]) > 1:
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
