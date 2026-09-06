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
# Database & Turso configuration
# ---------------------------------------------------------------------------

DB_PATH = "movies.sqlite"


def get_secret(key: str) -> Optional[str]:
    """Fetch Streamlit secret values with an environment variable fallback."""

    if hasattr(st, "secrets") and key in st.secrets:
        return st.secrets[key]
    return os.getenv(key)


def is_turso_configured() -> bool:
    """Return True if Turso database credentials are provided."""
    return bool(get_secret("TURSO_DATABASE_URL") and get_secret("TURSO_AUTH_TOKEN"))


class TursoRow:
    """Dictionary-accessible, tuple-accessible row wrapper for Turso."""

    def __init__(self, cols: List[str], values: List[Any]):
        self._dict = dict(zip(cols, values))
        self._values = tuple(values)

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, int):
            return self._values[key]
        return self._dict[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self._dict.get(key, default)

    def keys(self):
        return self._dict.keys()

    def values(self):
        return self._dict.values()

    def items(self):
        return self._dict.items()

    def __contains__(self, key: str) -> bool:
        return key in self._dict

    def __iter__(self):
        return iter(self._dict)

    def __len__(self) -> int:
        return len(self._dict)

    def __repr__(self) -> str:
        return f"<TursoRow {self._dict}>"


class TursoCursor:
    """DB-API compatible cursor wrapping libsql-client."""

    def __init__(self, client: Any):
        self._client = client
        self._rows: List[TursoRow] = []
        self._idx = 0
        self.rowcount = -1
        self.description = None

    def execute(self, sql: str, params: Any = ()):
        if isinstance(params, (list, tuple)):
            clean_params = list(params)
        elif params is None:
            clean_params = []
        else:
            clean_params = [params]

        res = self._client.execute(sql, clean_params)
        cols = list(res.columns) if hasattr(res, "columns") else []
        self._rows = [TursoRow(cols, r) for r in res.rows] if hasattr(res, "rows") else []
        self._idx = 0
        self.rowcount = getattr(res, "rows_affected", len(self._rows))
        self.description = [(col, None, None, None, None, None, None) for col in cols]
        return self

    def executemany(self, sql: str, seq_of_params: Any):
        if not seq_of_params:
            return self
        import libsql_client
        stmts = [
            libsql_client.Statement(sql, list(p) if isinstance(p, (list, tuple)) else [p])
            for p in seq_of_params
        ]
        self._client.batch(stmts)
        self._rows = []
        self._idx = 0
        return self

    def fetchone(self) -> Optional[TursoRow]:
        if self._idx < len(self._rows):
            row = self._rows[self._idx]
            self._idx += 1
            return row
        return None

    def fetchall(self) -> List[TursoRow]:
        remaining = self._rows[self._idx:]
        self._idx = len(self._rows)
        return remaining

    def fetchmany(self, size: Optional[int] = None) -> List[TursoRow]:
        n = size if size is not None else 1
        sub = self._rows[self._idx:self._idx + n]
        self._idx += len(sub)
        return sub

    def close(self):
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


class TursoConnection:
    """DB-API compatible connection wrapping libsql-client over HTTPS."""

    def __init__(self, url: str, auth_token: str):
        import libsql_client
        clean_url = url.replace("libsql://", "https://")
        self._client = libsql_client.create_client_sync(url=clean_url, auth_token=auth_token)

    def cursor(self) -> TursoCursor:
        return TursoCursor(self._client)

    def execute(self, sql: str, params: Any = ()):
        cur = self.cursor()
        return cur.execute(sql, params)

    def executemany(self, sql: str, seq_of_params: Any):
        cur = self.cursor()
        return cur.executemany(sql, seq_of_params)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        try:
            self._client.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


def get_db_connection():
    """Return a live Turso connection if configured, otherwise local SQLite connection."""
    if is_turso_configured():
        url = get_secret("TURSO_DATABASE_URL")
        token = get_secret("TURSO_AUTH_TOKEN")
        return TursoConnection(url, token)

    import sqlite3
    db_path = Path(DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


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
