from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import requests
from bs4 import BeautifulSoup

LETTERBOXD_RSS = "https://letterboxd.com/{username}/rss/"
LETTERBOXD_BASE = "https://letterboxd.com"
STAR = chr(9733)
HALF = chr(189)


@dataclass(slots=True)
class LetterboxdProfile:
    username: str
    fetched_at: str
    watchlist: list[dict[str, Any]]
    films: list[dict[str, Any]]
    likes: list[dict[str, Any]]
    diary: list[dict[str, Any]]
    activity: list[dict[str, Any]]
    sources: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class LetterboxdSourceProbe:
    source: str
    url: str
    fetched_at: str
    status_code: Optional[int]
    response_bytes: int
    content_type: str
    final_url: str
    redirected: bool
    is_html: bool
    requires_login: bool
    public: bool
    parseable: bool
    pagination_hint: bool
    film_link_count: int
    rating_hint: bool
    item_count: int
    items: list[dict[str, Any]]
    pages: list[dict[str, Any]]
    notes: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class LetterboxdProbeReport:
    username: str
    fetched_at: str
    sources: dict[str, LetterboxdSourceProbe]
    profile: LetterboxdProfile

    def to_dict(self) -> dict[str, Any]:
        return {
            "username": self.username,
            "fetched_at": self.fetched_at,
            "sources": {name: probe.to_dict() for name, probe in self.sources.items()},
            "profile": self.profile.to_dict(),
        }


def parse_rating(title: str) -> Optional[float]:
    prefix, separator, suffix = title.rpartition(" - ")
    if not separator or not suffix:
        return None
    if any(ch not in {STAR, HALF} for ch in suffix):
        return None
    full = suffix.count(STAR)
    half = 0.5 if suffix.endswith(HALF) else 0.0
    return full + half


def clean_title(title: str) -> str:
    prefix, separator, suffix = title.rpartition(" - ")
    if separator and suffix and all(ch in {STAR, HALF} for ch in suffix):
        return prefix.strip()
    return title.strip()


def normalize_film_link(link: str) -> str:
    if not link:
        return ""
    if link.startswith("http://") or link.startswith("https://"):
        return link
    if link.startswith("/"):
        return f"{LETTERBOXD_BASE}{link}"
    return f"{LETTERBOXD_BASE}/{link.lstrip('/')}"


def parse_rss_items(xml_bytes: bytes) -> list[dict[str, Any]]:
    root = ET.fromstring(xml_bytes)
    items: list[dict[str, Any]] = []

    for item in root.findall("./channel/item"):
        title_raw = item.findtext("title") or ""
        link = item.findtext("link") or ""
        published = item.findtext("pubDate") or ""
        year_match = re.search(r",\s*(\d{4})(?:\s+-\s+|$)", title_raw)

        items.append(
            {
                "title": clean_title(title_raw),
                "year": int(year_match.group(1)) if year_match else None,
                "rating": parse_rating(title_raw),
                "published": published,
                "link": link,
                "film_link": normalize_film_link(link),
            }
        )

    return items


def extract_grid_items(html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    items: list[dict[str, Any]] = []
    seen_links: set[str] = set()

    for card in soup.select('div.react-component[data-target-link]'):
        film_path = card.get("data-target-link") or ""
        if not film_path or film_path in seen_links:
            continue
        seen_links.add(film_path)

        li = card.find_parent("li")
        image = card.select_one("img.image[alt]")
        title = (image.get("alt") if image else "") or card.get("data-item-full-display-name") or ""
        title = title.strip()
        if not title:
            continue

        year_match = re.search(r"\((\d{4})\)\s*$", card.get("data-item-full-display-name") or title)
        rating_node = li.select_one("p.poster-viewingdata span.rating") if li else None
        like_node = li.select_one("p.poster-viewingdata span.like") if li else None
        uid_node = li.select_one("p.poster-viewingdata") if li else None
        raw_uid = uid_node.get("data-item-uid") if uid_node else None

        items.append(
            {
                "title": title,
                "year": int(year_match.group(1)) if year_match else None,
                "rating": rating_node.get_text(" ", strip=True) if rating_node else None,
                "liked": bool(like_node),
                "film_link": normalize_film_link(film_path),
                "poster_url": normalize_film_link(card.get("data-poster-url") or ""),
                "details_endpoint": normalize_film_link(card.get("data-details-endpoint") or ""),
                "item_uid": raw_uid,
            }
        )

    return items


DEFAULT_BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}


def fetch_url(
    url: str,
    session: Optional[requests.Session] = None,
    timeout: int = 15,
    referer: Optional[str] = None,
) -> requests.Response:
    http = session or requests.Session()
    http.trust_env = False
    headers = dict(DEFAULT_BROWSER_HEADERS)
    if referer:
        headers["Referer"] = referer
    return http.get(
        url,
        timeout=timeout,
        headers=headers,
    )


def fetch_rss_items(
    username: str,
    session: Optional[requests.Session] = None,
    timeout: int = 15,
) -> list[dict[str, Any]]:
    response = fetch_url(LETTERBOXD_RSS.format(username=username), session=session, timeout=timeout)
    response.raise_for_status()
    return parse_rss_items(response.content)


def build_profile(
    username: str,
    *,
    rss_items: Optional[list[dict[str, Any]]] = None,
    watchlist: Optional[list[dict[str, Any]]] = None,
    films: Optional[list[dict[str, Any]]] = None,
    likes: Optional[list[dict[str, Any]]] = None,
    activity: Optional[list[dict[str, Any]]] = None,
    source_notes: Optional[dict[str, Any]] = None,
) -> LetterboxdProfile:
    rss_provided = rss_items is not None
    watchlist_provided = watchlist is not None
    films_provided = films is not None
    likes_provided = likes is not None
    activity_provided = activity is not None

    rss_items = list(rss_items or [])
    watchlist = list(watchlist or [])
    films = list(films or [])
    likes = list(likes or [])
    activity = list(activity or [])

    diary_count = len(rss_items)
    highly_rated_count = sum(1 for item in rss_items if (item.get("rating") or 0) >= 4.5)

    sources = {
        "rss": {
            "available": rss_provided,
            "count": diary_count,
        },
        "watchlist": {
            "available": watchlist_provided,
            "count": len(watchlist),
        },
        "films": {
            "available": films_provided,
            "count": len(films),
        },
        "likes": {
            "available": likes_provided,
            "count": len(likes),
        },
        "activity": {
            "available": activity_provided,
            "count": len(activity),
        },
    }

    if source_notes:
        sources.update(source_notes)

    return LetterboxdProfile(
        username=username,
        fetched_at=datetime.now().astimezone().isoformat(),
        watchlist=watchlist,
        films=films,
        likes=likes,
        diary=rss_items,
        activity=activity,
        sources={
            **sources,
            "summary": {
                "watched_count": len(films),
                "diary_count": diary_count,
                "highly_rated_count": highly_rated_count,
                "watchlist_count": len(watchlist),
                "likes_count": len(likes),
            },
        },
    )


def profile_summary(profile: LetterboxdProfile) -> list[str]:
    summary = profile.sources.get("summary", {})

    def format_section(section: str, label: str) -> str:
        section_info = profile.sources.get(section, {})
        if not section_info.get("available"):
            return f"{label}: unavailable"
        return f"{label}: {section_info.get('count', 0)} films"

    return [
        f"Username: {profile.username}",
        format_section("watchlist", "Watchlist"),
        format_section("likes", "Likes"),
        f"Watched: {summary.get('watched_count', 0)} films",
        f"Highly rated: {summary.get('highly_rated_count', 0)} films",
        f"Recent diary: {summary.get('diary_count', 0)} films",
    ]


def write_json(payload: dict[str, Any], output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return output_path
