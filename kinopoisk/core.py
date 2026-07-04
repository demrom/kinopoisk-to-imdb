"""Shared core for the Kinopoisk scrapers.

Holds everything the scrapers need: the Movie model, the HTTP session and cookie
handling, and output helpers. The scrapers (kinopoisk.ratings,
kinopoisk.content_lists, kinopoisk.stars) are thin siblings on top of this.

Cookies come from the browser by default via fetch_cookies (rookiepy);
--cookie / --cookie-file / KINOPOISK_COOKIE override.

Authentication:
  These pages are served to a logged-in browser session. The scraper reuses your
  own session cookies and the standard headers a browser sends, so pages load the
  same as in your browser. It does not solve or circumvent captchas — without a
  valid session Kinopoisk shows its usual bot check and the scraper stops.

Dependencies: requests, beautifulsoup4, lxml (+ rookiepy for browser cookies).
"""

from __future__ import annotations

import csv
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.kinopoisk.ru"

# Env var names for secrets (so they don't leak into argv / shell history).
COOKIE_ENV = "KINOPOISK_COOKIE"  # the full Cookie string from the browser
UA_ENV = "KINOPOISK_UA"  # optional: custom User-Agent

# The standard request headers a Chrome browser sends, so requests look like the
# ones your own browser makes. These don't defeat any bot check — with a valid
# session the page is served, without one Kinopoisk returns its usual captcha.
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/149.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8,"
        "application/signed-exchange;v=b3;q=0.7"
    ),
    "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
    "Cache-Control": "max-age=0",
    "Priority": "u=0, i",
    "Referer": f"{BASE_URL}/",
    "Sec-Ch-Ua": '"Chromium";v="149", "Not)A;Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"macOS"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

ID_RE = re.compile(r"/(film|series)/(\d+)/")


@dataclass
class Movie:
    # --- identity / card data ---
    kp_id: int | None = None
    kind: str | None = None  # film | series
    url: str | None = None
    title: str | None = None
    year: str | None = None
    genre: str | None = None
    user_rating: int | None = None
    user_rating_sentiment: str | None = None  # positive | neutral | negative
    poster: str | None = None
    alt: str | None = None

    # --- extra metadata from content-list cards (empty for ratings) ---
    original_title: str | None = None
    countries: list[str] = field(default_factory=list)
    detail_genres: list[str] = field(default_factory=list)
    directors: list[str] = field(default_factory=list)
    actors: list[str] = field(default_factory=list)
    duration_min: int | None = None

    # --- content-list context (filled when scraped from a /mykp/folders/ list) ---
    list_id: int | None = None
    list_name: str | None = None
    list_position: int | None = None  # 1-based position within the list
    added_at: str | None = None  # when the movie was added to the list


# --------------------------------------------------------------------------- #
# HTTP session & cookies
# --------------------------------------------------------------------------- #
def resolve_cookie(
        cli_cookie: str | None = None,
        cli_cookie_file: str | None = None,
        *,
        use_browser: bool = True,
        browsers: list[str] | None = None,
        verbose: bool = False,
) -> str | None:
    """Where to get the Cookie from.

    Priority: --cookie > --cookie-file > env KINOPOISK_COOKIE > browser (rookiepy
    via fetch_cookies). Returns None only if everything failed.
    """
    if cli_cookie:
        return cli_cookie
    if cli_cookie_file:
        return Path(cli_cookie_file).read_text(encoding="utf-8").strip()
    env_cookie = os.environ.get(COOKIE_ENV)
    if env_cookie:
        return env_cookie

    if not use_browser:
        return None

    # Last resort: pull live cookies from the browser. Imported lazily so callers
    # that pass cookies explicitly never need fetch_cookies/rookiepy installed.
    try:
        from fetch_cookies import get_cookie_string
    except ImportError as e:
        print(f"WARNING: cannot import fetch_cookies ({e}); no cookies", file=sys.stderr)
        return None
    try:
        cookie = get_cookie_string("kinopoisk", browsers=browsers, verbose=verbose)
        print("Cookie: pulled from the browser via fetch_cookies", file=sys.stderr)
        return cookie
    except Exception as e:  # noqa: BLE001 — browser locked / not logged in / etc.
        print(f"WARNING: failed to pull cookies from the browser: {e}", file=sys.stderr)
        return None


def make_session(
        cookie: str | None = None, user_agent: str | None = None
) -> requests.Session:
    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)
    ua = user_agent or os.environ.get(UA_ENV)
    if ua:
        session.headers["User-Agent"] = ua
    if cookie:
        session.headers["Cookie"] = cookie
    else:
        print(
            f"WARNING: no Cookie (no {COOKIE_ENV}, --cookie, --cookie-file, or "
            "browser cookies). Kinopoisk will most likely serve a captcha.",
            file=sys.stderr,
        )
    return session


def fetch(
        session: requests.Session,
        url: str,
        *,
        retries: int = 3,
        pause: float = 2.0,
        timeout: int = 30,
) -> str:
    """Download a page. Detects captcha and network failures, retries."""
    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            resp = session.get(url, timeout=timeout)
            if resp.status_code in (403, 429) or "showcaptcha" in resp.url:
                raise RuntimeError(
                    f"Looks like anti-bot/captcha (status={resp.status_code}, "
                    f"url={resp.url})"
                )
            resp.raise_for_status()
            return resp.text
        except Exception as err:  # noqa: BLE001 — retry on any failure
            last_err = err
            if attempt < retries:
                time.sleep(pause * attempt)
    raise RuntimeError(f"Failed to download {url}: {last_err}")


# --------------------------------------------------------------------------- #
# HTML
# --------------------------------------------------------------------------- #
def make_soup(html: str) -> BeautifulSoup:
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:  # noqa: BLE001 — in case lxml is missing
        return BeautifulSoup(html, "html.parser")


# --------------------------------------------------------------------------- #
# Saving
# --------------------------------------------------------------------------- #
# These work on any list of dataclasses (Movie, Person, ...) via asdict().
def save_json(records: list, path: Path) -> None:
    data = [asdict(r) for r in records]
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def save_csv(records: list, path: Path) -> None:
    if not records:
        path.write_text("", encoding="utf-8")
        return
    rows = [asdict(r) for r in records]
    fields = list(rows[0].keys())
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {k: ("; ".join(v) if isinstance(v, list) else v) for k, v in row.items()}
            )


def save_records(records: list, path: Path, fmt: str) -> None:
    """Dispatch to save_json/save_csv by format ('json' or 'csv')."""
    if fmt == "csv":
        save_csv(records, path)
    else:
        save_json(records, path)


def slugify(name: str) -> str:
    """Filename-safe slug, keeping cyrillic letters: 'Буду смотреть' -> 'буду-смотреть'."""
    s = name.strip().lower()
    s = re.sub(r"[^\w]+", "-", s, flags=re.UNICODE)  # \w keeps cyrillic letters
    s = re.sub(r"-+", "-", s).strip("-")
    return s or "list"


# --------------------------------------------------------------------------- #
# Shared CLI helpers
# --------------------------------------------------------------------------- #
def add_http_args(parser, *, pause_default: float = 2.0) -> None:
    """Add the cookie-source and --pause flags shared by both scrapers."""
    parser.add_argument("--cookie", help="Cookie string from the browser")
    parser.add_argument("--cookie-file", help="file with the Cookie string")
    parser.add_argument(
        "--browser", action="append", dest="browsers", metavar="NAME",
        help="browser to read cookies from (arc/chrome/...); repeatable",
    )
    parser.add_argument(
        "--no-browser-cookie", action="store_true",
        help="do not auto-pull cookies from the browser",
    )
    parser.add_argument(
        "--pause", type=float, default=pause_default,
        help="pause between requests, sec",
    )


def session_from_args(args) -> requests.Session:
    """Build a session from the flags added by add_http_args."""
    cookie = resolve_cookie(
        args.cookie,
        args.cookie_file,
        use_browser=not args.no_browser_cookie,
        browsers=args.browsers,
    )
    return make_session(cookie=cookie)
