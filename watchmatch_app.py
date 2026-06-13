import streamlit as st
import random
import string
import requests
from typing import Dict, List, Set
import streamlit.components.v1 as components

# Fetch TMDB API key from secrets
try:
    TMDB_API_KEY = st.secrets["TMDB_API_KEY"]
except Exception:
    import os
    TMDB_API_KEY = os.getenv("TMDB_API_KEY", "")

TMDB_BASE_URL = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w500"

# Hardcoded FI providers for prototype
PROVIDERS = {
    "Netflix": 8,
    "Amazon Prime Video": 119,
    "Disney Plus": 337,
    "HBO Max": 1899,
    "Viaplay": 76,
    "Ruutu": 338,
    "Yle Areena": 323
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
    "Thriller": 53
}

# Global in-memory storage for the single watch party
@st.cache_resource
def get_global_session() -> dict:
    return {
        "users": {},
        "genre": None,
        "movies": [],
        "state": "SETUP", # SETUP, RATING, ROUND_2
        "match": None,
        "page_offset": 1
    }

lobby = get_global_session()

def fetch_movies(genre_id: int, provider_ids: List[int], start_page: int = 1) -> List[dict]:
    if not provider_ids:
        provider_str = ""
    else:
        provider_str = "|".join(map(str, provider_ids))
    
    movies = []
    for page in [start_page, start_page + 1]:
        params = {
            "api_key": TMDB_API_KEY,
            "language": "en-US",
            "watch_region": "FI",
            "with_watch_providers": provider_str,
            "with_genres": genre_id,
            "sort_by": "popularity.desc",
            "page": page
        }
        res = requests.get(f"{TMDB_BASE_URL}/discover/movie", params=params)
        if res.status_code == 200:
            movies.extend(res.json().get("results", []))
        if len(movies) >= 24:
            break
    return movies[:24]

# App Layout
st.set_page_config(page_title="WatchMatch", layout="wide", initial_sidebar_state="collapsed")

@st.dialog("Help & Instructions")
def show_help_dialog():
    st.markdown("""
    **Welcome to WatchMatch!**
    - **Step 1:** Enter your name and select your streaming services.
    - **Step 2:** Wait for your friends to join the same screen.
    - **Step 3:** Pick a Genre and click 'Start Matching!'.
    - **Phase 1:** Rate movies with 🍿. 5 is the best!
    - **Phase 2:** The top movies are shown. Vote 'Yes' to finalize your choice.
    - **No Matches?** If no one can agree on a movie from the 1st round, you can repeat the process for a 2nd and then a 3rd round with new movies. If that doesn't work, then it is no longer a movie night—instead, you go out for a walk!
    
    *Shortcuts:*
    - Press **H** to show this help menu.
    - Press **D** to toggle Dark Mode.
    """)

if st.button("HiddenHelp"):
    show_help_dialog()

components.html(
    """
    <script>
    const doc = window.parent.document;
    
    // Hide the button container
    const buttons = Array.from(doc.querySelectorAll('button'));
    const helpBtn = buttons.find(b => b.innerText.includes('HiddenHelp'));
    if (helpBtn) {
        const btnContainer = helpBtn.closest('div[data-testid="stButton"]');
        if (btnContainer) btnContainer.style.display = 'none';
    }
    
    if (!doc.window_watchmatch_keys_bound) {
        doc.addEventListener('keydown', function(e) {
            // Ignore if typing in an input field
            if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
            
            if (e.key.toLowerCase() === 'h') {
                const btns = Array.from(doc.querySelectorAll('button'));
                const hBtn = btns.find(b => b.innerText.includes('HiddenHelp'));
                if (hBtn) hBtn.click();
            }
            
            if (e.key.toLowerCase() === 'd') {
                doc.documentElement.classList.toggle('custom-dark-mode');
            }
        });
        doc.window_watchmatch_keys_bound = true;
    }
    </script>
    """,
    height=0,
    width=0
)

st.markdown("""
<style>
/* Instant Dark Mode via CSS Inversion */
html.custom-dark-mode {
    filter: invert(1) hue-rotate(180deg);
}
/* Re-invert images and emojis so they look normal */
html.custom-dark-mode img, html.custom-dark-mode video, html.custom-dark-mode iframe {
    filter: invert(1) hue-rotate(180deg);
}
</style>
""", unsafe_allow_html=True)

st.title("🍿 WatchMatch")
st.markdown("Find the perfect movie for your group, available on your streaming services in Finland!")

# Session state initialization
if "user_name" not in st.session_state:
    st.session_state.user_name = None

user_name = st.session_state.user_name

# --- SCREEN 1: JOIN WATCH PARTY ---
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
    
    # Check if there is a match
    if lobby["match"]:
        st.success("🎉 IT'S A MATCH! 🎉")
        st.markdown(f"### You are all watching: **{lobby['match']['title']}** tonight!")
        if lobby['match'].get('poster_path'):
            st.image(f"{TMDB_IMAGE_BASE}{lobby['match']['poster_path']}", width=300)
        st.markdown(lobby['match'].get('overview', ''))
        
        if st.button("Start Over / New Search"):
            lobby["match"] = None
            lobby["state"] = "SETUP"
            lobby["movies"] = []
            for u in lobby["users"].values():
                u["votes"] = {}
                u["round2_votes"] = {}
                u["ready"] = False
                u["index"] = 0
            st.rerun()
        st.stop()

    # --- SCREEN 2: SETUP LOBBY ---
    if lobby["state"] == "SETUP":
        st.subheader("Watch Party Setup")
        st.markdown("Waiting for everyone to join...")
        
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
            lobby["page_offset"] = 1
            # Fetch movies
            p_ids = [PROVIDERS[s] for s in combined_services]
            movies = fetch_movies(GENRES[genre_name], p_ids, lobby["page_offset"])
            if movies:
                lobby["movies"] = movies
                lobby["state"] = "RATING"
                st.rerun()
            else:
                st.error("No movies found for this combination of genre and streaming services.")
                
    # --- SCREEN 3: RATING ---
    elif lobby["state"] == "RATING":
        st.subheader(f"Genre: {lobby['genre']} - Phase 1: Rating")
        st.markdown("Rate the movies from 0 to 5 🍿. When you are done, click 'Submit Ratings' at the bottom.")
        
        user_data = lobby["users"][user_name]
        movies_to_show = lobby["movies"]
        
        if not movies_to_show:
            st.info("No movies found.")
            if st.button("Go Back to Setup"):
                lobby["state"] = "SETUP"
                st.rerun()
            st.stop()
            
        cols_per_row = 6
        for i in range(0, len(movies_to_show), cols_per_row):
            cols = st.columns(cols_per_row)
            for j in range(cols_per_row):
                if i + j < len(movies_to_show):
                    movie = movies_to_show[i + j]
                    with cols[j]:
                        if movie.get('poster_path'):
                            st.image(f"{TMDB_IMAGE_BASE}{movie['poster_path']}", use_container_width=True)
                        else:
                            st.write("No poster available")
                            
                        # Rating Segmented Control (Natively responsive, 1 row)
                        current_vote = user_data["votes"].get(movie["id"], 0)
                        
                        options = [1, 2, 3, 4, 5]
                        def format_popcorns(x):
                            return f"{x} 🍿"
                            
                        # If current_vote is 0, default should be None so no segment is selected
                        default_val = current_vote if current_vote > 0 else None
                        
                        new_vote = st.segmented_control(
                            "Rating", 
                            options, 
                            selection_mode="single", 
                            default=default_val, 
                            format_func=format_popcorns, 
                            key=f"rate_{movie['id']}", 
                            label_visibility="collapsed"
                        )
                        
                        # new_vote is None if user deselects
                        final_vote = new_vote if new_vote is not None else 0
                        
                        if final_vote != current_vote:
                            lobby["users"][user_name]["votes"][movie["id"]] = final_vote
                            st.rerun()
                        
                        # Info Expander
                        with st.expander("ℹ️ Info"):
                            st.write(f"**{movie['title']}** ({movie.get('release_date', '')[:4]})")
                            st.caption(f"⭐ {movie.get('vote_average', 'N/A')}")
                            st.write(movie.get('overview', 'No overview.'))
                            
        st.markdown("---")
        if user_data["ready"]:
            st.success("Waiting for other friends to finish rating...")
            if st.button("Refresh Status"):
                st.rerun()
        else:
            if st.button("Submit Ratings", type="primary"):
                lobby["users"][user_name]["ready"] = True
                
                # Check if all users are ready
                all_ready = all(u["ready"] for u in lobby["users"].values())
                if all_ready and len(lobby["users"]) > 1:
                    lobby["state"] = "ROUND_2"
                st.rerun()

    # --- SCREEN 4: ROUND 2 ---
    elif lobby["state"] == "ROUND_2":
        st.subheader("Phase 2: Final Vote")
        st.markdown("Here are the top rated movies. Vote **Yes** to any movie you'd watch. If everyone votes Yes, it's a Match!")
        
        # Calculate scores
        movie_scores = []
        for movie in lobby["movies"]:
            total_score = sum(u["votes"].get(movie["id"], 0) for u in lobby["users"].values())
            if total_score > 0:
                movie_scores.append((total_score, movie))
                
        movie_scores.sort(key=lambda x: x[0], reverse=True)
        top_movies = [m[1] for m in movie_scores[:6]]
        
        user_data = lobby["users"][user_name]
        
        def load_new_movies():
            lobby["page_offset"] += 2
            combined_services = set()
            for u in lobby["users"].values():
                combined_services.update(u["services"])
            p_ids = [PROVIDERS[s] for s in combined_services]
            lobby["movies"] = fetch_movies(GENRES[lobby["genre"]], p_ids, lobby["page_offset"])
            for u in lobby["users"].values():
                u["votes"] = {}
                u["round2_votes"] = {}
                u["ready"] = False
            lobby["state"] = "RATING"
            st.rerun()

        if not top_movies:
            st.warning("Nobody liked any movies! Ready for a new batch?")
            colA, colB = st.columns(2)
            with colA:
                if st.button("Load New Movies", type="primary"):
                    load_new_movies()
            with colB:
                if st.button("Start Over (Setup)"):
                    lobby["state"] = "SETUP"
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
                        if movie.get('poster_path'):
                            st.image(f"{TMDB_IMAGE_BASE}{movie['poster_path']}", use_container_width=True)
                        else:
                            st.write("No poster available")
                            
                        # Final Yes/No Checkbox
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
                            st.write(movie.get('overview', 'No overview.'))
                            
        st.markdown("---")
        st.markdown("Not feeling any of these?")
        colA, colB = st.columns(2)
        with colA:
            if st.button("Load New Movies", type="primary"):
                load_new_movies()
        with colB:
            if st.button("Start Over (Setup)"):
                lobby["state"] = "SETUP"
                st.rerun()
