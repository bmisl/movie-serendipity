from __future__ import annotations

import argparse
import random
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import requests

from letterboxd_profile import (
    LetterboxdProbeReport,
    LetterboxdSourceProbe,
    build_profile,
    extract_grid_items,
    fetch_rss_items,
    fetch_url,
    write_json,
)

SOURCE_PATHS = {
    "films": "/{username}/films/",
    "watchlist": "/{username}/watchlist/",
    "likes": "/{username}/likes/films/",
    "activity": "/{username}/activity/",
    "rss": "/{username}/rss/",
}
PAGINATED_SOURCES = {"films", "watchlist", "activity"}
MAX_PAGES = 20
PAGE_SIZE_HINT = 72


@dataclass(slots=True)
class PageSnapshot:
    page: int
    url: str
    status_code: Optional[int]
    response_bytes: int
    final_url: str
    redirected: bool
    item_count: int
    notes: list[str]


@dataclass(slots=True)
class SourceSnapshot:
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

    def to_probe(self) -> LetterboxdSourceProbe:
        return LetterboxdSourceProbe(**asdict(self))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Probe public Letterboxd sources for a single username and save a normalized report."
    )
    parser.add_argument("username", help="Letterboxd username to inspect")
    parser.add_argument(
        "--output-dir",
        default="letterboxd_profiles",
        help="Directory where the report JSON should be written",
    )
    return parser.parse_args()


def probe_source(username: str, source: str) -> SourceSnapshot:
    url = f"https://letterboxd.com{SOURCE_PATHS[source].format(username=username)}"
    fetched_at = datetime.now().astimezone().isoformat()
    notes: list[str] = []
    all_items: list[dict[str, Any]] = []
    pages: list[dict[str, Any]] = []
    first_status_code: Optional[int] = None
    first_response_bytes = 0
    first_content_type = ""
    first_final_url = url
    first_redirected = False
    first_is_html = False
    first_requires_login = False
    first_public = False
    first_parseable = False
    first_pagination_hint = False
    first_film_link_count = 0
    first_rating_hint = False

    try:
        page = 1
        session = requests.Session()
        session.trust_env = False
        prev_url = None
        while page <= MAX_PAGES:
            page_url = url if page == 1 else f"https://letterboxd.com/{username}/{source}/page/{page}/"
            if page > 1:
                time.sleep(0.3 + 0.3 * random.random())
            response = fetch_url(page_url, session=session, referer=prev_url)
            prev_url = page_url
            status_code = response.status_code
            response_bytes = len(response.content or b"")
            final_url = response.url
            redirected = final_url.rstrip("/") != page_url.rstrip("/")
            body = response.text or ""
            lowered = body.lower()
            content_type = response.headers.get("Content-Type", "")
            is_html = "html" in content_type.lower() or "<html" in lowered
            requires_login = status_code in {401, 403} or "sign in" in lowered or "log in" in lowered
            public = status_code == 200 and not requires_login
            pagination_hint = bool(
                re.search(r'rel=["\']next["\']', body, re.I)
                or re.search(r'\bnext page\b', body, re.I)
                or (source != "rss" and re.search(r'/page/\d+/', body))
            )
            film_link_count = len(re.findall(r"/film/", body))
            rating_hint = bool(re.search(r"[★½]", body))
            page_items = extract_grid_items(body) if source in PAGINATED_SOURCES else []
            page_notes: list[str] = []

            if page == 1:
                first_status_code = status_code
                first_response_bytes = response_bytes
                first_content_type = content_type
                first_final_url = final_url
                first_redirected = redirected
                first_is_html = is_html
                first_requires_login = requires_login
                first_public = public
                first_parseable = source == "rss" or bool(page_items) or response_bytes > 0
                first_pagination_hint = pagination_hint
                first_film_link_count = film_link_count
                first_rating_hint = rating_hint

            if source == "rss":
                if status_code == 200:
                    try:
                        all_items = fetch_rss_items(username)
                        pages.append(
                            {
                                "page": 1,
                                "url": page_url,
                                "status_code": status_code,
                                "response_bytes": response_bytes,
                                "final_url": final_url,
                                "redirected": redirected,
                                "item_count": len(all_items),
                                "notes": page_notes,
                            }
                        )
                    except Exception as exc:
                        page_notes.append(f"rss-parse-error={exc}")
                break

            pages.append(
                {
                    "page": page,
                    "url": page_url,
                    "status_code": status_code,
                    "response_bytes": response_bytes,
                    "final_url": final_url,
                    "redirected": redirected,
                    "item_count": len(page_items),
                    "notes": page_notes,
                }
            )

            if not page_items:
                if page == 1:
                    notes.append("no-grid-items-found")
                break

            all_items.extend(page_items)
            if not pagination_hint or len(page_items) < PAGE_SIZE_HINT:
                break
            page += 1

        if pages:
            last_page = pages[-1]
            if last_page.get("status_code") and last_page["status_code"] != 200:
                notes.append(f"terminal-status={last_page['status_code']}")
        if response.headers.get("Retry-After"):
            notes.append(f"retry-after={response.headers.get('Retry-After')}")
    except Exception as exc:
        notes.append(f"error={exc}")

    return SourceSnapshot(
        source=source,
        url=url,
        fetched_at=fetched_at,
        status_code=first_status_code,
        response_bytes=first_response_bytes,
        content_type=first_content_type,
        final_url=first_final_url,
        redirected=first_redirected,
        is_html=first_is_html,
        requires_login=first_requires_login,
        public=first_public,
        parseable=first_parseable,
        pagination_hint=first_pagination_hint,
        film_link_count=first_film_link_count,
        rating_hint=first_rating_hint,
        item_count=len(all_items),
        items=all_items,
        pages=pages,
        notes=notes,
    )


def main() -> int:
    args = parse_args()
    username = args.username.strip()
    output_dir = Path(args.output_dir)
    output_path = output_dir / f"{username}.json"

    source_snapshots = {name: probe_source(username, name) for name in SOURCE_PATHS}

    profile = build_profile(
        username,
        rss_items=source_snapshots["rss"].items,
        watchlist=source_snapshots["watchlist"].items,
        films=source_snapshots["films"].items,
        activity=source_snapshots["activity"].items,
    )
    report = LetterboxdProbeReport(
        username=username,
        fetched_at=datetime.now().astimezone().isoformat(),
        sources={name: snapshot.to_probe() for name, snapshot in source_snapshots.items()},
        profile=profile,
    )

    write_json(report.to_dict(), output_path)

    print("Letterboxd probe")
    print("-----")
    for name in SOURCE_PATHS:
        snapshot = source_snapshots[name]
        status = snapshot.status_code if snapshot.status_code is not None else "ERR"
        print(f"{name:10} {status!s:>4} {snapshot.response_bytes:>6} bytes {snapshot.item_count:4} items {snapshot.final_url}")
        if snapshot.pages:
            for page in snapshot.pages[:3]:
                print(f"  page {page['page']:<2} status={page['status_code']} items={page['item_count']} url={page['url']}")
    print(f"\nSaved: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
