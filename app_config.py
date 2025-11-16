"""Shared configuration helpers for the Streamlit apps."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Optional

import requests
import streamlit as st


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
