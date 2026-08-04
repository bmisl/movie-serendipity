"""Shared configuration helpers for the Streamlit apps."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Dict, Optional

import requests
import streamlit as st

# ---------------------------------------------------------------------------
# Region & streaming-service configuration
# ---------------------------------------------------------------------------

#: Mapping from human-readable country name to TMDB region code.
REGIONS: Dict[str, str] = {
    "Finland": "FI",
    "Denmark": "DK",
    "Iceland": "IS",
}

#: Streaming services available per region, keyed by TMDB provider ID.
REGION_PROVIDERS: Dict[str, Dict[str, int]] = {
    "FI": {
        "Netflix": 8,
        "Amazon Prime Video": 119,
        "Disney Plus": 337,
        "HBO Max": 1899,
        "Viaplay": 76,
        "Apple TV+": 350,
        "Ruutu": 338,
        "Yle Areena": 323,
        "Viddla": 539,
    },
    "DK": {
        "Netflix": 8,
        "Amazon Prime Video": 119,
        "Disney Plus": 337,
        "HBO Max": 1899,
        "Viaplay": 76,
        "Apple TV+": 350,
        "TV 2 Play": 398,
        "DRTV": 620,
    },
    "IS": {
        "Netflix": 8,
        "Amazon Prime Video": 119,
        "Disney Plus": 337,
        "HBO Max": 1899,
        "Viaplay": 76,
        "Apple TV+": 350,
        "RÚV": 2674,
    },
}

#: Genre names to TMDB genre IDs. ``None`` means "no filter" (all genres).
GENRES: Dict[str, Optional[int]] = {
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

# ---------------------------------------------------------------------------
# Database path
# ---------------------------------------------------------------------------

DB_PATH = "movies.sqlite"


def get_secret(key: str) -> Optional[str]:
    """Fetch Streamlit secret values with an environment variable fallback."""

    if hasattr(st, "secrets") and key in st.secrets:
        return st.secrets[key]
    return os.getenv(key)


def build_drive_download_url(file_id: str) -> str:
    """Return a download URL for a publicly shared Google Drive file."""

    return f"https://drive.google.com/uc?export=download&id={file_id}"


@lru_cache(maxsize=4)
def ensure_database_file(db_path: str = "movies.sqlite") -> str:
    """Download the SQLite database when it isn't available locally."""

    database_path = Path(db_path)
    if database_path.exists():
        return str(database_path)

    download_url = get_secret("DB_DOWNLOAD_URL")
    if not download_url:
        file_id = get_secret("DB_FILE_ID")
        if file_id:
            download_url = build_drive_download_url(file_id)

    if not download_url:
        st.error(
            "The movie database is missing. Set DB_DOWNLOAD_URL or DB_FILE_ID to a "
            "publicly shared link so the app can download movies.sqlite."
        )
        st.stop()

    try:
        with st.spinner("Downloading movie database…"):
            response = requests.get(download_url, timeout=60)
            response.raise_for_status()
            content = response.content
    except requests.RequestException:  # pragma: no cover - user-facing messaging
        st.error(
            "Unable to download movies.sqlite. Check DB_DOWNLOAD_URL/DB_FILE_ID and "
            "ensure the link is accessible."
        )
        st.stop()

    database_path.parent.mkdir(parents=True, exist_ok=True)
    with open(database_path, "wb") as handle:
        handle.write(content)

    return str(database_path)
