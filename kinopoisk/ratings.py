"""Scrape a Kinopoisk user's rated movies (the "voted-watched" list).

Walks the listing pages (/user/<id>/movies/voted-watched/) and pulls everything
available right in the card: link, the user's rating and its "color"
(positive/neutral/negative), title, year, genre, poster.

User id:
  In live mode the id is auto-detected from your logged-in session (the /mykp/
  redirect), so you never pass or commit a real id. Override with --user or the
  KINOPOISK_USER env var to scrape someone else's public list.

Output goes to out/kinopoisk/ratings.json by default (override with -o).

Examples:
    # live: cookies auto-pulled from the browser, own user id auto-detected
    python -m kinopoisk.ratings

    # someone else's public list by explicit id, custom output path
    python -m kinopoisk.ratings --user <USER_ID> -o out/other.json

    # parse a single saved listing page (no cookies needed)
    python -m kinopoisk.ratings --from-file page1.html
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

import requests

from .core import (
    BASE_URL,
    ID_RE,
    Movie,
    add_http_args,
    fetch,
    make_soup,
    save_records,
    session_from_args,
)

USER_ENV = "KINOPOISK_USER"  # Kinopoisk user id
USER_ID_RE = re.compile(r"/user/(\d+)/")
# Every logged-in /mykp/ page embeds `getUserId = function () { return <id>; }`.
GET_USER_ID_RE = re.compile(r"getUserId\s*=\s*function\s*\(\s*\)\s*\{\s*return\s+(\d+)")


# --------------------------------------------------------------------------- #
# User id
# --------------------------------------------------------------------------- #
def detect_user_id(session: requests.Session, *, timeout: int = 30, pause: float = 2.0) -> str:
    """Detect the logged-in user's own id, without ever typing/committing it.

    Fast path: /mykp/ sometimes 302-redirects straight to /user/<id>/. Otherwise
    fall back to the `getUserId` snippet embedded in any /mykp/ page's header JS.
    Requires valid cookies.
    """
    resp = session.get(f"{BASE_URL}/mykp/", timeout=timeout, allow_redirects=False)
    if "showcaptcha" not in resp.url:
        m = USER_ID_RE.search(resp.headers.get("Location", ""))
        if m:
            return m.group(1)

    html = fetch(session, f"{BASE_URL}/mykp/folders/1/", pause=pause)
    m = GET_USER_ID_RE.search(html)
    if m:
        return m.group(1)
    raise RuntimeError(
        "Could not detect the user id (are you logged in?). "
        f"Pass --user or set {USER_ENV}."
    )


def resolve_user_id(
        session: requests.Session, cli_user: str | None = None, *, pause: float = 2.0
) -> str:
    """Where to get the user id: --user > env KINOPOISK_USER > auto-detect."""
    user = cli_user or os.environ.get(USER_ENV)
    if user:
        return str(user)
    user_id = detect_user_id(session, pause=pause)
    print(f"user id: auto-detected {user_id} from the session", file=sys.stderr)
    return user_id


# --------------------------------------------------------------------------- #
# Listing parsing
# --------------------------------------------------------------------------- #
def _sentiment_from_classes(classes: list[str]) -> str | None:
    for cls in classes:
        if "valuePositive" in cls:
            return "positive"
        if "valueNegative" in cls:
            return "negative"
        if "valueNeutral" in cls:
            return "neutral"
    return None


def _split_subtitle(text: str) -> tuple[str | None, str | None]:
    """'2024, anime' -> ('2024', 'anime'). Also handles 'Title. 2024, anime'."""
    if not text:
        return None, None
    text = text.strip()
    # alt variant with the title up front: take the part after '. '
    if ". " in text and not text[:4].isdigit():
        text = text.split(". ", 1)[1]
    year, genre = None, None
    m = re.search(r"(\d{4})", text)
    if m:
        year = m.group(1)
    if "," in text:
        genre = text.split(",", 1)[1].strip() or None
    return year, genre


def parse_item(item, base_url: str = BASE_URL) -> Movie:
    """Parse a single movie card from the listing."""
    movie = Movie()

    # link / id / kind — take the first link to /film/ or /series/
    link = item.select_one('a[href*="/film/"], a[href*="/series/"]')
    if link and link.get("href"):
        href = link["href"]
        movie.url = urljoin(base_url, href)
        m = ID_RE.search(href)
        if m:
            movie.kind = m.group(1)
            movie.kp_id = int(m.group(2))

    # user's rating and its "color"
    rating_span = item.select_one(".styles_value__NKB8e")
    if rating_span:
        raw = rating_span.get_text(strip=True)
        if raw.isdigit():
            movie.user_rating = int(raw)
        sentiment_holder = item.select_one(".styles_rating__of_L5")
        if sentiment_holder:
            movie.user_rating_sentiment = _sentiment_from_classes(
                sentiment_holder.get("class", [])
            )

    # title
    title_el = item.select_one(".styles_title__NNXAn")
    if title_el:
        movie.title = title_el.get_text(strip=True) or None

    # "year, genre" subtitle
    subtitle_el = item.select_one(".styles_subtitle__8QGYo")
    if subtitle_el:
        movie.year, movie.genre = _split_subtitle(subtitle_el.get_text(strip=True))

    # poster + alt (alt duplicates title/year/genre — kept as raw material)
    img = item.select_one("img")
    if img:
        movie.alt = img.get("alt")
        src = img.get("src")
        if src:
            movie.poster = urljoin(base_url, src)
        if movie.title is None and movie.alt:
            movie.title = movie.alt.split(".", 1)[0].strip()
        if movie.year is None and movie.alt:
            movie.year, movie.genre = _split_subtitle(movie.alt)

    return movie


def parse_listing(html: str, base_url: str = BASE_URL) -> list[Movie]:
    """All movie cards from a single listing page."""
    soup = make_soup(html)
    items = soup.select("div.styles_item__S5nUo")
    return [parse_item(it, base_url) for it in items]


def find_last_page(html: str) -> int:
    """Number of the last pagination page (from ?page=N links)."""
    soup = make_soup(html)
    pages = [1]
    for a in soup.select('a[href*="page="]'):
        m = re.search(r"page=(\d+)", a.get("href", ""))
        if m:
            pages.append(int(m.group(1)))
    return max(pages)


# --------------------------------------------------------------------------- #
# Scraping
# --------------------------------------------------------------------------- #
def listing_url(user_id: str, page: int) -> str:
    base = f"{BASE_URL}/user/{user_id}/movies/voted-watched/"
    return base if page == 1 else f"{base}?page={page}"


def scrape_listing(
        session: requests.Session,
        user_id: str,
        *,
        max_pages: int | None = None,
        pause: float = 2.0,
) -> list[Movie]:
    """Collect all cards across all listing pages."""
    first_html = fetch(session, listing_url(user_id, 1), pause=pause)
    last_page = find_last_page(first_html)
    if max_pages:
        last_page = min(last_page, max_pages)

    movies = parse_listing(first_html)
    print(f"page 1/{last_page}: {len(movies)} cards", file=sys.stderr)

    for page in range(2, last_page + 1):
        time.sleep(pause)
        html = fetch(session, listing_url(user_id, page), pause=pause)
        page_movies = parse_listing(html)
        movies.extend(page_movies)
        print(f"page {page}/{last_page}: {len(page_movies)} cards", file=sys.stderr)
    return movies


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])

    # Offline sources (mutually exclusive). Live mode is the default when neither
    # is given.
    src = p.add_mutually_exclusive_group()
    src.add_argument("--from-file", help="parse a single saved listing page")
    src.add_argument("--html-dir", help="directory with saved listing pages (*.html)")

    p.add_argument(
        "--user",
        help=f"Kinopoisk user id (default: env ${USER_ENV} or auto-detect from session)",
    )
    p.add_argument(
        "-o", "--out", default="out/kinopoisk/ratings.json",
        help="where to save (default: out/kinopoisk/ratings.json)",
    )
    p.add_argument(
        "--format", choices=["json", "csv"],
        help="output format (default: from the --out extension)",
    )
    p.add_argument("--max-pages", type=int, help="limit the number of listing pages")
    add_http_args(p)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fmt = args.format or ("csv" if out_path.suffix.lower() == ".csv" else "json")

    movies: list[Movie] = []

    if args.from_file:
        movies = parse_listing(Path(args.from_file).read_text(encoding="utf-8"))
    elif args.html_dir:
        for html_file in sorted(Path(args.html_dir).glob("*.html")):
            movies.extend(parse_listing(html_file.read_text(encoding="utf-8")))
    else:
        session = session_from_args(args)
        try:
            user_id = resolve_user_id(session, args.user, pause=args.pause)
        except Exception as e:  # noqa: BLE001 — clear message instead of a traceback
            print(f"ERROR: {e}", file=sys.stderr)
            return 1
        movies = scrape_listing(
            session, user_id, max_pages=args.max_pages, pause=args.pause
        )

    save_records(movies, out_path, fmt)
    print(f"Done: {len(movies)} movies -> {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
