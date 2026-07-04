"""Export a Kinopoisk user's content lists (movies, series, anime, ...) to files.

Kinopoisk calls these "folders" in the UI and URLs (/mykp/folders/<id>/), but
each one is really a personal list of content — favourites, watchlist, anime,
and so on. Each list becomes its own JSON (or CSV) file, so other scripts can
import a specific list on its own.

List discovery:
  The list catalog (id, name, count) is read from the sidebar present on any list
  page (/mykp/movies/ redirects through SSO for scripted requests). A list page
  omits itself from its sidebar, so we union the sidebars of a few seed lists —
  see --seed.

Each list is saved to out/kinopoisk/content-lists/<id>-<slug>.json by default
(override the directory with --out-dir).

Examples:
    # print the catalog (id, name, count), do not scrape
    python -m kinopoisk.content_lists --list

    # scrape every list into out/kinopoisk/content-lists/<id>-<slug>.json
    python -m kinopoisk.content_lists --all

    # scrape only specific lists (by their /mykp/folders/<id>/ id)
    python -m kinopoisk.content_lists --id 6 --id 1102

    # save as CSV instead of JSON
    python -m kinopoisk.content_lists --all --format csv
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

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
    slugify,
)

# System lists that always exist (1=Избранное, 2=Смотреть в кино). We seed
# discovery from several lists and union their sidebars, because a list's own
# page does NOT list itself as a plain nav link — only the *other* lists.
DEFAULT_SEED_LISTS = [1, 2]

# Kinopoisk caps ?limit= at 100 for these lists (150+ returns HTTP 400); 100
# items per page cuts the request count ~4x vs the default 25.
PAGE_LIMIT = 100

PAGES_FROM_TO_RE = re.compile(r"(\d+)\s*[—–-]\s*(\d+)\s+из\s+(\d+)")
YEAR_RE = re.compile(r"\((\d{4})")
DURATION_MIN_RE = re.compile(r"(\d+)\s*мин")
LIST_PATH_RE = re.compile(r"/mykp/folders/(\d+)/$")
COUNT_IN_PARENS_RE = re.compile(r"\((\d+)\)")


@dataclass
class ContentList:
    id: int
    name: str
    count: int | None = None


# --------------------------------------------------------------------------- #
# List discovery
# --------------------------------------------------------------------------- #
def _plain_list_id(href: str) -> int | None:
    """List id if `href` is a plain sidebar link, else None.

    A list page also carries genre/country filter links like
    /mykp/folders/6/?genres=1750 (where "аниме" is a *genre*, not the list name)
    and ?page=N pagination. Those must be excluded — only a bare
    /mykp/folders/<id>/ (optionally ?format=full) is a real list entry.
    """
    u = urlparse(href)
    m = LIST_PATH_RE.search(u.path)
    if not m:
        return None
    if set(parse_qs(u.query).keys()) - {"format"}:
        return None
    return int(m.group(1))


def parse_list_sidebar(html: str) -> list[ContentList]:
    """Read the list catalog (id, name, count) from a list page's sidebar.

    Note: a list page shows every list *except* the current one, so a single page
    never yields the full set — discover_lists() unions several.
    """
    soup = make_soup(html)
    lists: dict[int, ContentList] = {}
    for a in soup.select('a[href*="/mykp/folders/"]'):
        lid = _plain_list_id(a.get("href", ""))
        if lid is None or lid in lists:
            continue
        name = a.get_text(" ", strip=True)
        if not name:
            continue
        # count usually sits in the parent as "Name (123)"
        parent_text = a.parent.get_text(" ", strip=True) if a.parent else ""
        cm = COUNT_IN_PARENS_RE.search(parent_text)
        count = int(cm.group(1)) if cm else None
        lists[lid] = ContentList(id=lid, name=name, count=count)
    return list(lists.values())


def discover_lists(
        session: requests.Session,
        *,
        seeds: list[int] | None = None,
        pause: float = 2.0,
) -> list[ContentList]:
    """Union the sidebars of several seed pages into the full list catalog.

    A list page omits itself from its sidebar, so seeding from the system lists
    (1, 2) — plus any explicit seed — captures every list including the seeds.
    """
    seeds = list(dict.fromkeys(seeds or DEFAULT_SEED_LISTS))
    lists: dict[int, ContentList] = {}
    errors: list[str] = []
    for i, sid in enumerate(seeds):
        if i:
            time.sleep(pause)
        try:
            html = fetch(session, list_url(sid, 1), pause=pause)
        except Exception as e:  # noqa: BLE001 — a dead seed shouldn't abort discovery
            errors.append(f"{sid}: {e}")
            continue
        for cl in parse_list_sidebar(html):
            cur = lists.get(cl.id)
            if cur is None or (cur.count is None and cl.count is not None):
                lists[cl.id] = cl
    if not lists:
        raise RuntimeError(
            "No content lists found in sidebars of seeds "
            f"{seeds}. Are you logged in? Errors: {errors or 'none'}"
        )
    return list(lists.values())


# --------------------------------------------------------------------------- #
# List page parsing
# --------------------------------------------------------------------------- #
def list_url(list_id: int | str, page: int = 1, limit: int = PAGE_LIMIT) -> str:
    # format=full renders the full metadata row (original title, year, runtime,
    # director, genres, cast) instead of just the poster+title; limit sets the
    # page size (max 100). Kinopoisk's URL segment for a list is "folders".
    base = f"{BASE_URL}/mykp/folders/{list_id}/?format=full&limit={limit}"
    return base if page <= 1 else f"{base}&page={page}"


def _parse_meta_span(text: str) -> tuple[str | None, str | None, int | None]:
    """'Equilibrium (2002) 107 мин.' -> ('Equilibrium', '2002', 107)."""
    year = None
    m = YEAR_RE.search(text)
    if m:
        year = m.group(1)
    duration = None
    m = DURATION_MIN_RE.search(text)
    if m:
        duration = int(m.group(1))
    original = text.split("(", 1)[0].strip() or None
    return original, year, duration


def parse_list_item(item, base_url: str = BASE_URL) -> Movie:
    """Parse one <li class="item"> row of a list page into a Movie."""
    movie = Movie()

    data_id = item.get("data-id")
    if data_id and data_id.isdigit():
        movie.kp_id = int(data_id)

    # position within the list
    number_el = item.select_one(".number")
    if number_el:
        num = number_el.get_text(strip=True)
        if num.isdigit():
            movie.list_position = int(num)

    # title link -> url, kind, title
    name_link = item.select_one("div.info a.name") or item.select_one(
        'div.info a[href*="/film/"], div.info a[href*="/series/"]'
    )
    if name_link and name_link.get("href"):
        href = name_link["href"]
        movie.url = urljoin(base_url, href)
        m = ID_RE.search(href)
        if m:
            movie.kind = m.group(1)
            if movie.kp_id is None:
                movie.kp_id = int(m.group(2))
        movie.title = name_link.get_text(" ", strip=True) or None

    info = item.select_one("div.info")
    if info:
        for span in info.find_all("span", recursive=False):
            if "last" in (span.get("class") or []):
                continue  # the "Афиша • Трейлеры • Кадры" nav row
            text = span.get_text(" ", strip=True)
            director_tag = span.find("i")
            if director_tag is not None and "реж" in director_tag.get_text():
                # "США, реж. Курт Уиммер" -> country + directors. The country is
                # the text before the <i>; the separator may be a comma or an
                # ellipsis ("США…" when countries are truncated).
                movie.directors = [
                    a.get_text(" ", strip=True)
                    for a in director_tag.select("a")
                    if a.get_text(strip=True)
                ]
                lead = "".join(span.find_all(string=True, recursive=False))
                country = re.sub(r"[,\s.…]+$", "", lead.strip())
                if country:
                    movie.countries = [country]
            elif text.startswith("(") and text.endswith(")"):
                # "(фантастика, боевик, триллер...)" -> genres
                inner = text.strip("()").replace("…", "").replace("...", "")
                movie.detail_genres = [g.strip() for g in inner.split(",") if g.strip()]
            elif span.select("a.lined"):
                # actors row
                movie.actors = [
                    a.get_text(" ", strip=True)
                    for a in span.select("a.lined")
                    if a.get_text(strip=True)
                ]
            elif movie.original_title is None and movie.year is None:
                # first plain span: "Equilibrium (2002) 107 мин."
                movie.original_title, movie.year, movie.duration_min = _parse_meta_span(text)

    # poster: real URL lives in the img title (src is a spacer gif)
    img = item.select_one("div.images img") or item.select_one("img")
    if img:
        movie.alt = img.get("alt") or movie.alt
        for candidate in (img.get("title"), img.get("src")):
            if candidate and candidate.startswith("http") and "spacer" not in candidate:
                movie.poster = candidate
                break

    # date added to the list — the small absolute-positioned span
    for span in item.find_all("span", recursive=False):
        txt = span.get_text(" ", strip=True)
        if re.fullmatch(r"\d{2}\.\d{2}\.\d{4}(,\s*\d{2}:\d{2})?", txt):
            movie.added_at = txt
            break

    return movie


def parse_list_page(html: str, base_url: str = BASE_URL) -> tuple[list[Movie], int | None]:
    """All title rows on a list page + the reported total (from 'X—Y из N')."""
    soup = make_soup(html)
    movies = [parse_list_item(it, base_url) for it in soup.select("li.item[data-id]")]

    total = None
    from_to = soup.select_one(".pagesFromTo")
    if from_to:
        m = PAGES_FROM_TO_RE.search(from_to.get_text(" ", strip=True))
        if m:
            total = int(m.group(3))
    return movies, total


# --------------------------------------------------------------------------- #
# Scraping a list across pages
# --------------------------------------------------------------------------- #
def scrape_list(
        session: requests.Session,
        content_list: ContentList,
        *,
        max_pages: int | None = None,
        limit: int = PAGE_LIMIT,
        pause: float = 2.0,
) -> list[Movie]:
    """Collect every title across all pages of one list (deduped by id)."""
    first_html = fetch(session, list_url(content_list.id, 1, limit), pause=pause)
    page_movies, total = parse_list_page(first_html)

    movies: list[Movie] = []
    seen: set[int] = set()

    def add(batch: list[Movie]) -> int:
        added = 0
        for m in batch:
            key = m.kp_id if m.kp_id is not None else id(m)
            if key in seen:
                continue
            seen.add(key)
            m.list_id = content_list.id
            m.list_name = content_list.name
            movies.append(m)
            added += 1
        return added

    added = add(page_movies)
    print(
        f"list {content_list.id} ({content_list.name}) page 1: {added} titles"
        + (f" / {total} total" if total else ""),
        file=sys.stderr,
    )

    page = 2
    while page_movies:
        if max_pages and page > max_pages:
            break
        if total is not None and len(movies) >= total:
            break
        time.sleep(pause)
        html = fetch(session, list_url(content_list.id, page, limit), pause=pause)
        page_movies, page_total = parse_list_page(html)
        if page_total is not None:
            total = page_total
        added = add(page_movies)
        if added == 0:
            break  # nothing new (past the end or a repeated page)
        print(
            f"list {content_list.id} ({content_list.name}) page {page}: {added} titles "
            f"(collected {len(movies)}" + (f"/{total}" if total else "") + ")",
            file=sys.stderr,
        )
        page += 1

    return movies


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
def list_out_path(out_dir: Path, content_list: ContentList, fmt: str) -> Path:
    ext = "csv" if fmt == "csv" else "json"
    return out_dir / f"{content_list.id}-{slugify(content_list.name)}.{ext}"


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sel = p.add_mutually_exclusive_group(required=True)
    sel.add_argument(
        "--id", action="append", dest="ids", metavar="ID",
        help="content-list id to scrape (repeatable)",
    )
    sel.add_argument("--all", action="store_true", help="scrape all discovered lists")
    sel.add_argument(
        "--list", action="store_true",
        help="only print the catalog (id, name, count) and exit",
    )

    p.add_argument(
        "--out-dir", default="out/kinopoisk/content-lists",
        help="directory for per-list files (default: out/kinopoisk/content-lists/)",
    )
    p.add_argument(
        "--format", choices=["json", "csv"], default="json",
        help="output format (default: json)",
    )
    p.add_argument("--max-pages", type=int, help="limit pages scraped per list")
    p.add_argument(
        "--limit", type=int, default=PAGE_LIMIT,
        help=f"titles per page / request (max 100; default {PAGE_LIMIT})",
    )
    p.add_argument(
        "--seed", type=int, action="append", dest="seeds", metavar="ID",
        help=(
            "extra list id to seed discovery from (repeatable); always unioned "
            f"with system lists {DEFAULT_SEED_LISTS}"
        ),
    )
    add_http_args(p)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    session = session_from_args(args)

    # Discover the catalog either way to attach real names/counts; explicitly
    # requested ids fall back to a bare ContentList if not in the catalog.
    seeds = (args.seeds or []) + DEFAULT_SEED_LISTS
    try:
        discovered = {cl.id: cl for cl in discover_lists(session, seeds=seeds, pause=args.pause)}
    except Exception as e:  # noqa: BLE001 — clear message instead of a traceback
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    if args.ids:
        targets = [
            discovered.get(int(lid), ContentList(id=int(lid), name=str(lid)))
            for lid in args.ids
        ]
    else:
        targets = list(discovered.values())

    if args.list:
        for cl in targets:
            print(f"{cl.id}\t{cl.count if cl.count is not None else '?'}\t{cl.name}")
        return 0

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    grand_total = 0
    for content_list in targets:
        try:
            movies = scrape_list(
                session, content_list, max_pages=args.max_pages,
                limit=args.limit, pause=args.pause,
            )
        except Exception as e:  # noqa: BLE001 — one list must not kill the run
            print(f"list {content_list.id} ({content_list.name}): ERROR {e}", file=sys.stderr)
            continue
        path = list_out_path(out_dir, content_list, args.format)
        save_records(movies, path, args.format)
        grand_total += len(movies)
        print(f"saved {len(movies)} titles -> {path}", file=sys.stderr)
        time.sleep(args.pause)

    print(f"Done: {grand_total} titles across {len(targets)} lists -> {out_dir}/", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
