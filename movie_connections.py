"""Streamlit app to explore collaboration paths between people in the catalogue."""

from __future__ import annotations

import os
import sqlite3
from collections import defaultdict, deque
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import requests
import streamlit as st

DB_PATH = "movies.sqlite"
BASE_URL = "https://www.omdbapi.com/"

st.set_page_config(
    page_title="Filmography Connection Explorer",
    layout="wide",
    initial_sidebar_state="collapsed",
)


def get_secret(key: str) -> Optional[str]:
    """Fetch configuration values from Streamlit secrets or the environment."""

    if hasattr(st, "secrets") and key in st.secrets:
        return st.secrets[key]
    return os.getenv(key)


def ensure_api_key(key: Optional[str], label: str) -> str:
    """Stop execution with a helpful message when a required key is missing."""

    if not key:
        st.error(
            f"Missing {label}. Add it to Streamlit secrets or set the {label} environment variable."
        )
        st.stop()
    return key


OMDB_API_KEY = ensure_api_key(get_secret("OMDB_API_KEY"), "OMDB_API_KEY")


@st.cache_data(show_spinner=False)
def fetch_omdb_poster(imdb_id: Optional[str]) -> Optional[str]:
    """Return the OMDb poster URL for the supplied IMDb identifier."""

    if not imdb_id:
        return None

    try:
        response = requests.get(
            BASE_URL,
            params={"i": imdb_id, "apikey": OMDB_API_KEY},
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException:
        return None

    if payload.get("Response") != "True":
        return None

    poster_url = payload.get("Poster")
    if poster_url and poster_url != "N/A":
        return poster_url
    return None


@st.cache_data(show_spinner=False)
def load_graph_data() -> Tuple[
    Dict[int, Dict[str, object]],
    Dict[int, Dict[str, str]],
    Dict[int, Tuple[int, ...]],
    Dict[int, Tuple[int, ...]],
    Dict[Tuple[int, int], Tuple[str, ...]],
]:
    """Load actors/directors and their movie collaborations from the database."""

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT
            m.id AS movie_id,
            m.title AS movie_title,
            m.year AS movie_year,
            m.imdb_id AS movie_imdb_id,
            p.id AS person_id,
            p.name AS person_name,
            mp.role AS role
        FROM movie_people AS mp
        JOIN people AS p ON p.id = mp.person_id
        JOIN movies AS m ON m.id = mp.movie_id
        WHERE mp.role IN ('Actor', 'Director')
    """
    ).fetchall()
    conn.close()

    person_details: Dict[int, Dict[str, object]] = {}
    movie_details: Dict[int, Dict[str, str]] = {}
    person_to_movies: Dict[int, set[int]] = defaultdict(set)
    movie_to_people: Dict[int, set[int]] = defaultdict(set)
    edge_roles: Dict[Tuple[int, int], set[str]] = defaultdict(set)

    for row in rows:
        person_id = int(row["person_id"])
        movie_id = int(row["movie_id"])
        role = row["role"] or ""

        if person_id not in person_details:
            person_details[person_id] = {
                "name": row["person_name"] or "Unknown person",
                "roles": set(),
            }
        person_details[person_id]["roles"].add(role)

        if movie_id not in movie_details:
            year_value = row["movie_year"]
            movie_details[movie_id] = {
                "title": row["movie_title"] or "Untitled",
                "year": str(year_value) if year_value else "",
                "imdb_id": row["movie_imdb_id"],
            }

        person_to_movies[person_id].add(movie_id)
        movie_to_people[movie_id].add(person_id)
        if role:
            edge_roles[(person_id, movie_id)].add(role)

    # Convert sets into sorted tuples for deterministic caching
    for person_id, details in person_details.items():
        role_set = details.get("roles")
        if isinstance(role_set, set):
            role_values = sorted(role for role in role_set if role)
            details["roles"] = tuple(role_values)

    sorted_movies = {
        movie_id: tuple(sorted(movie_to_people[movie_id], key=lambda pid: person_details.get(pid, {}).get("name", "")))
        for movie_id in movie_to_people
    }
    sorted_people = {
        person_id: tuple(sorted(person_to_movies[person_id], key=lambda mid: movie_details.get(mid, {}).get("title", "")))
        for person_id in person_to_movies
    }

    edge_role_map = {
        key: tuple(sorted(values)) if values else tuple()
        for key, values in edge_roles.items()
    }

    return person_details, movie_details, sorted_people, sorted_movies, edge_role_map


def format_person_label(person_id: int, person_details: Dict[int, Dict[str, object]]) -> str:
    """Return a human-friendly label for dropdown options."""

    details = person_details.get(person_id)
    if not details:
        return "Unknown"

    name = str(details.get("name", "Unknown"))
    roles_value = details.get("roles")
    roles: str = ""
    if isinstance(roles_value, Sequence) and not isinstance(roles_value, (str, bytes)):
        roles = "/".join(str(role) for role in roles_value if role)
    if roles:
        return f"{name} — {roles}"
    return name


def find_shortest_paths(
    start_person: int,
    target_person: int,
    person_to_movies: Dict[int, Tuple[int, ...]],
    movie_to_people: Dict[int, Tuple[int, ...]],
    limit: int = 3,
) -> List[List[Tuple[str, int]]]:
    """Breadth-first search that returns up to *limit* shortest collaboration paths."""

    if start_person == target_person:
        return []

    start_node = ("person", start_person)
    goal_node = ("person", target_person)
    queue: deque[Tuple[Tuple[str, int], List[Tuple[str, int]]]] = deque()
    queue.append((start_node, [start_node]))

    visited_depth: Dict[str, Dict[int, int]] = {"person": {start_person: 0}, "movie": {}}
    solutions: List[List[Tuple[str, int]]] = []
    shortest_length: Optional[int] = None

    while queue:
        node, path = queue.popleft()
        if shortest_length is not None and len(path) > shortest_length:
            break

        node_type, node_id = node
        if node == goal_node and len(path) > 1:
            if shortest_length is None:
                shortest_length = len(path)
            solutions.append(path)
            if len(solutions) >= limit:
                break
            continue

        if node_type == "person":
            neighbours: Iterable[Tuple[str, int]] = (
                ("movie", movie_id) for movie_id in person_to_movies.get(node_id, ())
            )
        else:
            neighbours = (
                ("person", person_id) for person_id in movie_to_people.get(node_id, ())
            )

        for neighbour in neighbours:
            if neighbour in path:
                continue
            depth = len(path)
            type_bucket = visited_depth.setdefault(neighbour[0], {})
            previous = type_bucket.get(neighbour[1])
            if previous is not None and previous < depth:
                continue
            type_bucket[neighbour[1]] = depth
            queue.append((neighbour, path + [neighbour]))

    return solutions


def build_graphviz(
    path: Sequence[Tuple[str, int]],
    person_details: Dict[int, Dict[str, object]],
    movie_details: Dict[int, Dict[str, str]],
) -> str:
    """Return a Graphviz diagram highlighting the supplied path."""

    lines: List[str] = [
        "graph G {",
        "  rankdir=LR;",
        "  overlap=false;",
        "  splines=true;",
        "  fontname='Helvetica';",
    ]

    def node_name(node: Tuple[str, int]) -> str:
        prefix = "p" if node[0] == "person" else "m"
        return f"{prefix}{node[1]}"

    for index, (node_type, node_id) in enumerate(path):
        if node_type == "person":
            details = person_details.get(node_id, {"name": "Unknown", "roles": ()})
            label = str(details.get("name", "Unknown"))
            roles_value = details.get("roles", ())
            roles = ""
            if isinstance(roles_value, Sequence) and not isinstance(roles_value, (str, bytes)):
                roles = ", ".join(str(role) for role in roles_value if role)
            if roles:
                label += f"\\n({roles})"
            fillcolor = "#d1e7dd" if index == 0 else "#cfe2ff" if index == len(path) - 1 else "#f8f9fa"
            lines.append(
                f"  {node_name((node_type, node_id))} [shape=ellipse, style=filled, fillcolor='{fillcolor}', label="
                f"\"{label}\"];"
            )
        else:
            details = movie_details.get(node_id, {"title": "Untitled", "year": ""})
            title = details.get("title", "Untitled")
            year = details.get("year")
            label = title if not year else f"{title}\\n({year})"
            lines.append(
                f"  {node_name((node_type, node_id))} [shape=box, style=filled, fillcolor='#fde2b2', label=\"{label}\"];"
            )

    for first, second in zip(path, path[1:]):
        lines.append(f"  {node_name(first)} -- {node_name(second)};")

    lines.append("}")
    return "\n".join(lines)


def format_movie(movie_id: int, movie_details: Dict[int, Dict[str, str]]) -> str:
    """Return a movie title with its year when available."""

    info = movie_details.get(movie_id, {"title": "Untitled", "year": ""})
    title = info.get("title", "Untitled")
    year = info.get("year")
    return f"{title} ({year})" if year else title


def describe_connection(
    path: Sequence[Tuple[str, int]],
    person_details: Dict[int, Dict[str, object]],
    movie_details: Dict[int, Dict[str, str]],
    edge_roles: Dict[Tuple[int, int], Tuple[str, ...]],
) -> List[str]:
    """Produce a human-readable list of connection steps for the provided path."""

    steps: List[str] = []
    for index in range(0, len(path) - 2, 2):
        person_id = path[index][1]
        movie_id = path[index + 1][1]
        partner_id = path[index + 2][1]

        person_name = str(person_details.get(person_id, {}).get("name", "Unknown"))
        partner_name = str(person_details.get(partner_id, {}).get("name", "Unknown"))
        movie_label = format_movie(movie_id, movie_details)

        left_roles = edge_roles.get((person_id, movie_id), ("Contributor",))
        right_roles = edge_roles.get((partner_id, movie_id), ("Contributor",))

        steps.append(
            f"**{person_name}** ({', '.join(left_roles)}) → *{movie_label}* → "
            f"**{partner_name}** ({', '.join(right_roles)})"
        )
    return steps


def render_path(
    title: str,
    path: Sequence[Tuple[str, int]],
    person_details: Dict[int, Dict[str, object]],
    movie_details: Dict[int, Dict[str, str]],
    edge_roles: Dict[Tuple[int, int], Tuple[str, ...]],
) -> None:
    """Render the graphical and textual representation of a connection path."""

    st.subheader(title)
    degrees = (len(path) - 1) // 2
    st.caption(f"Degrees of separation: {degrees}")

    dot = build_graphviz(path, person_details, movie_details)
    st.graphviz_chart(dot, width="stretch")

    movie_nodes = [node_id for node_type, node_id in path if node_type == "movie"]
    if movie_nodes:
        st.markdown("**Posters along the path**")
        poster_columns = st.columns(len(movie_nodes))
        for column, movie_id in zip(poster_columns, movie_nodes):
            details = movie_details.get(movie_id, {})
            poster_url = details.get("omdb_poster")
            if poster_url is None:
                imdb_id = details.get("imdb_id")
                poster_url = fetch_omdb_poster(imdb_id)
                details["omdb_poster"] = poster_url

            caption = format_movie(movie_id, movie_details)
            if poster_url:
                column.image(poster_url, use_column_width=True, caption=caption)
            else:
                column.markdown(f"**{caption}**\n\nPoster unavailable.")

    st.markdown("**Chain**")
    for step in describe_connection(path, person_details, movie_details, edge_roles):
        st.markdown(f"- {step}")


def load_people_order(person_details: Dict[int, Dict[str, object]]) -> Tuple[int, ...]:
    """Return people sorted alphabetically for dropdowns."""

    return tuple(
        sorted(
            person_details.keys(),
            key=lambda pid: str(person_details[pid].get("name", "")).lower(),
        )
    )


def main() -> None:
    st.title("Filmography Connection Explorer")
    st.write(
        "Select two actors or directors from your catalogue to uncover the shortest collaboration "
        "paths between them."
    )

    (
        person_details,
        movie_details,
        person_to_movies,
        movie_to_people,
        edge_roles,
    ) = load_graph_data()

    if not person_details:
        st.error("No actor or director information is available in the database.")
        return

    people_order = load_people_order(person_details)
    format_func = lambda pid: format_person_label(pid, person_details)

    col_a, col_b = st.columns(2)
    with col_a:
        person_a = st.selectbox(
            "Person #1",
            options=people_order,
            format_func=format_func,
            key="person_a",
        )
    with col_b:
        person_b = st.selectbox(
            "Person #2",
            options=people_order,
            format_func=format_func,
            index=min(1, len(people_order) - 1),
            key="person_b",
        )

    if person_a == person_b:
        st.info("Pick two different people to explore their collaboration path.")
        return

    paths = find_shortest_paths(person_a, person_b, person_to_movies, movie_to_people, limit=3)

    if not paths:
        st.warning("No collaboration path was found between the selected people.")
        return

    labels = ["Shortest path", "Alternative path 2", "Alternative path 3"]
    tabs = st.tabs(labels[: len(paths)])
    for tab, path, label in zip(tabs, paths, labels):
        with tab:
            render_path(label, path, person_details, movie_details, edge_roles)


if __name__ == "__main__":
    main()
