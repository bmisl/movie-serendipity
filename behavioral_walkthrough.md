# WatchMatch v2: Step-by-Step Behavioral Walkthrough

This document simulates a complete, end-to-end user session of [`watchmatch_app.py`](file:///c:/Users/birgi/Code/Python/movie-serendipity/watchmatch_app.py), demonstrating how three friends (**Birgir**, **Asta**, and **Hugleikur**) use WatchMatch to choose a movie for Friday night.

---

## 🎬 Scenario Overview
- **Location / Region**: Finland (`FI`)
- **Group Members**: Birgir, Asta, and Hugleikur
- **Goal**: Find a movie everyone is excited to watch, available on their streaming services, enriched by their Letterboxd taste profiles.

---

## 1. Onboarding & Letterboxd Profile Sync

```text
User enters name → selects streaming services → inputs Letterboxd username → clicks "Join Room"
```

1. Birgir opens `http://localhost:8501` in his browser.
2. He sees the header **🍿 WatchMatch v2** and selects the **Region**: `Finland`.
3. In the **Join the Watch Party** form, Birgir enters:
   - **Name**: `Birgir`
   - **Streaming Services**: `Netflix`, `Max`
   - **Personalization**: `☑ Sync my Letterboxd taste & watchlist for group recommendations`
   - **Letterboxd Username**: `birgirm`
4. Birgir clicks **Join Room**.
5. **Behind the Scenes**:
   - WatchMatch initiates a session-scoped background sync using [`letterboxd_source_probe.py`](file:///c:/Users/birgi/Code/Python/movie-serendipity/letterboxd_source_probe.py).
   - Passes browser-equivalent HTTP headers, session cookies, and dynamic referers to bypass rate limits.
   - Fetches Birgir's public watched collection (72 films) and watchlist (12 films).
   - Attaches the snapshot to `lobby["users"]["Birgir"]["letterboxd"]`.

---

## 2. Room Lobby & Group Arrival

```text
Room Lobby displays active participants, individual services, and Letterboxd sync badges.
```

1. **Asta** opens the app in a second browser window and joins:
   - **Name**: `Asta`
   - **Services**: `Netflix`, `Disney Plus`
   - **Letterboxd Username**: `asta07maria`
   - *Synced*: 500+ watched films, 15 watchlist items.
2. **Hugleikur** joins in a third browser window:
   - **Name**: `Hugleikur`
   - **Services**: `Netflix`, `Max`
   - *Letterboxd*: Skipped (Normal participation without Letterboxd).
3. The live room state updates across all screens:
   ```text
   Participants:
   • Birgir: Netflix, Max · 🎬 Letterboxd (72 watched, 12 watchlist)
   • Asta: Netflix, Disney Plus · 🎬 Letterboxd (500+ watched, 15 watchlist)
   • Hugleikur: Netflix, Max

   Combined Group Services: Netflix, Max, Disney Plus
   ```

---

## 3. Candidate Pool Generation (Letterboxd + `movies.sqlite` + TMDB)

```text
Host selects Genre → Clicks "Start Group Match" → System builds personalized candidate pool
```

1. Birgir selects **Genre**: `Sci-Fi` and clicks **Start Group Match**.
2. **Behind the Scenes Candidate Engine**:
   - Gathers all Letterboxd watchlist titles from Birgir and Asta.
   - Resolves titles to TMDB IDs via `resolve_letterboxd_item()`:
     - Checks local database [`movies.sqlite`](file:///c:/Users/birgi/Code/Python/movie-serendipity/movies.sqlite) table `letterboxd_cache` first.
     - If missing, queries TMDB Search API, saves the resolved TMDB ID & details into `movies.sqlite` for future instant lookups.
   - Calculates candidate match scores:
     - $+50$ points for films on multiple watchlists (e.g. *Dune: Part Two* on both Birgir & Asta's watchlists).
     - $+25$ points for highly rated films ($\ge 4.0$).
   - Fetches TMDB Sci-Fi movies streaming on `Netflix`, `Max`, or `Disney Plus` in Finland.
   - Merges and ranks the candidate pool, storing the top 24 cards in `lobby["movie_pool"]`.

---

## 4. Phase 1: Independent Movie Ranking (Pick 5)

```text
Each user receives 24 movie cards → Ranks top 5 choices (Ranks 1 to 5) → Submits Ranking
```

1. All participants see a 6-column grid of 24 movie cards.
2. Each movie card displays:
   - High-resolution poster image
   - Release year & TMDB rating
   - Contextual badge pills (e.g. `<span class="badge-pill">🔖 Watchlist (2)</span>`)
   - Expandable **ℹ️ Info** drawer with synopsis
3. **Birgir's Ranking**:
   - Rank 1 🥇: *Dune: Part Two*
   - Rank 2 🥈: *Interstellar*
   - Rank 3 🥉: *The Substance*
   - Rank 4 🎖️: *Arrival*
   - Rank 5 🎖️: *Spider-Man: Across the Spider-Verse*
   - Clicks **Submit Ranking**. Screen displays: *"Waiting for other friends to finish ranking..."*.
4. Asta and Hugleikur submit their independent rankings.

---

## 5. Phase 2: Final Vote & Consensus Building

```text
System identifies top 5 overall group choices → Users vote YES on acceptable movies
```

1. Once all three friends submit, WatchMatch transitions to **Phase 2: Final Vote**.
2. Displays the top 5 highest-voted overall movies across the room:
   ```text
   1. Dune: Part Two (14 Total Popcorns 🍿)
   2. Interstellar (12 Total Popcorns 🍿)
   3. Arrival (8 Total Popcorns 🍿)
   4. Spider-Man: Across the Spider-Verse (5 Total Popcorns 🍿)
   5. The Substance (3 Total Popcorns 🍿)
   ```
3. Each user checks `Vote Yes` for any movie they would be happy to watch tonight.
4. Birgir checks **Yes** for *Dune: Part Two* and *Interstellar*.
5. Asta checks **Yes** for *Dune: Part Two*.
6. Hugleikur checks **Yes** for *Dune: Part Two*.

---

## 6. Match Celebration & Recommendation Outcome

```text
All participants voted YES on Dune: Part Two → Unanimous Consensus Reached!
```

1. As soon as Hugleikur clicks **Yes** for *Dune: Part Two*, the app instantly triggers the match state across all screens:
   ```text
   🎉 IT'S A MATCH! 🎉
   You are all watching: Dune: Part Two tonight!
   ```
2. Displays movie poster, overview, and exact streaming availability:
   `📺 Available on your services: Max`

---

## 7. Bonus: Solo SQLite Database Browsing

```text
User toggles to "Solo SQLite Browser" mode for single-user movie discovery
```

1. Later, Birgir opens WatchMatch for single-user browsing and clicks the top tab **🔍 Solo SQLite Browser**.
2. He enters `"Inception"` in the search input and sets **Min TMDB Rating** slider to `7.5`.
3. The app queries `movies.sqlite` directly in real-time, fetching matching rows without hitting external APIs:
   - Displays movie cards, release dates, ratings, and plot overviews instantly from local storage.
