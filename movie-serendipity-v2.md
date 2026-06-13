Act as an expert full-stack developer. I want to build a lightweight, highly responsive web application prototype called "WatchMatch." The core concept is "Tinder for movies" but optimized for groups/couples trying to decide what to watch in Finland. 

Please write the complete code for a single-page prototype (using Streamlit or a clean HTML/JS/Tailwind CSS stack—choose whichever is faster for a working demo) that implements the following core loop:

1. SESSION SETUP:
- The user can select which streaming services they have access to in Finland (Netflix, Disney+, Prime Video, HBO Max).
- A button to "Create Lobby" which generates a unique session ID (can be mocked for now) and allows a second user to join the same session.

2. FILTERING LOGIC (Mock Data):
- Create a hardcoded mock dataset of 30 popular movies/shows. Include fields for: Title, Poster URL, Genres, and an array of Streaming Providers (e.g., ['Netflix', 'Disney+']).
- The app must dynamically filter this dataset to only show movies available on the selected streaming services.

3. THE SWIPING INTERFACE:
- A clean, mobile-first card interface showing one movie poster and title at a time.
- Two distinct buttons: "❌ Pass" and "❤️ Watch".
- Keyboard shortcuts: Left arrow for Pass, Right arrow for Watch.

4. MATCHING MECHANISM:
- Simulate two users (User A and User B) voting on the same filtered stack. 
- When BOTH users click "❤️ Watch" on the same movie, trigger a celebratory overlay/modal that says: "It's a Match! You are watching [Movie Title] tonight!"

Provide the complete, clean, documented code and instructions on how to run it locally. Focus heavily on a smooth, low-friction user experience.