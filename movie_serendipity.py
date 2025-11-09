# serendipity_v3.py
import streamlit as st
import requests
import random
import statistics

API_KEY = "8f375814"
BASE_URL = "https://www.omdbapi.com/"

st.title("🎬 Serendipitous Movie Picker")

# 1️⃣ Step One: Type of filter
filter_type = st.selectbox("Choose a type:", ["Genre", "Actor", "Director"])

# 2️⃣ Step Two: Dynamic dropdown based on type
options = {
    "Genre": ["Action", "Adventure", "Comedy", "Drama", "Horror", "Sci-Fi", "Romance", "Thriller"],
    "Actor": ["Tom Hanks", "Scarlett Johansson", "Leonardo DiCaprio", "Denzel Washington", "Natalie Portman"],
    "Director": ["Christopher Nolan", "Steven Spielberg", "Ridley Scott", "Quentin Tarantino", "Greta Gerwig"],
}
selection = st.selectbox(f"Select a {filter_type.lower()}:", options[filter_type])

# 3️⃣ Fetch movies from OMDb (broad search)
def search_movies(term):
    """Fetch up to ~20 movie results by search term"""
    url = f"{BASE_URL}?s={term}&apikey={API_KEY}"
    res = requests.get(url)
    data = res.json()
    if data.get("Response") == "True":
        return data["Search"]
    return []

movies = search_movies(selection)

# 4️⃣ Compute median year
years = []
for m in movies:
    year = m.get("Year", "")
    if year.isdigit():
        years.append(int(year))

if years:
    median_year = int(statistics.median(years))
    st.caption(f"📅 Median release year for {selection}: {median_year}")

# 5️⃣ Buttons for older / newer
col1, col2 = st.columns(2)

def show_random_movie(filtered_movies):
    if not filtered_movies:
        st.warning("No movies match that range.")
        return
    pick = random.choice(filtered_movies)
    imdb_id = pick["imdbID"]
    detail_url = f"{BASE_URL}?i={imdb_id}&apikey={API_KEY}"
    detail = requests.get(detail_url).json()

    st.image(detail["Poster"], width=200)
    st.markdown(f"**{detail['Title']} ({detail['Year']})** ⭐ {detail['imdbRating']}")
    st.caption(detail["Genre"])
    st.write(detail["Plot"])

if col1.button("⬅️ Older"):
    older_movies = [m for m in movies if m["Year"].isdigit() and int(m["Year"]) <= median_year]
    show_random_movie(older_movies)

if col2.button("➡️ Newer"):
    newer_movies = [m for m in movies if m["Year"].isdigit() and int(m["Year"]) > median_year]
    show_random_movie(newer_movies)
