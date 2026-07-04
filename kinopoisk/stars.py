"""Export a Kinopoisk user's saved people (stars) into separate files.

Same shape as kinopoisk.content_lists, but for people the user follows/rated at
/mykp/stars/list/type/<id>/ (actors, actresses, directors, favourites, ...).
Each type becomes its own JSON (or CSV) file.

Type discovery:
  The type list (id, name, count) is read from the sidebar of any stars page. A
  page omits its own type, so we union the sidebars of a few seed types — see
  --seed-type.

Each type is saved to out/kinopoisk/stars/<id>-<slug>.json by default.

Examples:
    # list all star types (id, name, count), do not scrape
    python -m kinopoisk.stars --list

    # scrape every type into out/kinopoisk/stars/<id>-<slug>.json
    python -m kinopoisk.stars --all

    # scrape only specific types (2=Актёры, 3=Актрисы, 4=Режиссёры)
    python -m kinopoisk.stars --type 2 --type 4
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

from .core import (
    BASE_URL,
    add_http_args,
    fetch,
    make_soup,
    save_records,
    session_from_args,
    slugify,
)

# System types that always exist (1=Избранное, 2=Актёры). We seed discovery from
# several types and union their sidebars, because a stars page does NOT list its
# own type as a plain nav link — only the *other* types.
DEFAULT_SEED_TYPES = [1, 2]

# Kinopoisk accepts perpage up to 200 for stars (300+ falls back to 25). Every
# star type is well under 200, so this is normally a single request per type.
STARS_PERPAGE = 200

PHOTO_HOST = "https://st.kp.yandex.net"  # person thumbnails live here

PAGES_FROM_TO_RE = re.compile(r"(\d+)\s*[—–-]\s*(\d+)\s+из\s+(\d+)")
NAME_ID_RE = re.compile(r"/name/(\d+)/")
TYPE_PATH_RE = re.compile(r"/mykp/stars/list/type/(\d+)/$")
BIRTH_DATE_RE = re.compile(r"(\d{1,2}\s+[А-Яа-яёЁ]+\s+\d{4})")
COUNT_IN_PARENS_RE = re.compile(r"\((\d+)\)")


@dataclass
class StarType:
    id: int
    name: str
    count: int | None = None


@dataclass
class Person:
    kp_id: int | None = None
    name: str | None = None  # Russian name
    original_name: str | None = None  # Latin / original name
    url: str | None = None
    birth_date: str | None = None  # e.g. "30 сентября 1982"
    photo: str | None = None
    alt: str | None = None  # img alt, e.g. "Киран Калкин (Kieran Culkin)"
    # which stars list this came from
    list_type_id: int | None = None
    list_type_name: str | None = None


# --------------------------------------------------------------------------- #
# Type discovery
# --------------------------------------------------------------------------- #
def _plain_type_id(href: str) -> int | None:
    """Type id if `href` is a plain sidebar link, else None.

    Excludes genre/country filter and pagination links (which carry extra path
    segments or a query string) — only a bare /mykp/stars/list/type/<id>/ counts.
    """
    u = urlparse(href)
    m = TYPE_PATH_RE.search(u.path)
    if not m or u.query:
        return None
    return int(m.group(1))


def parse_type_sidebar(html: str) -> list[StarType]:
    """Read the star-type sidebar (id, name, count) from a stars page.

    A page lists every type *except* the current one, so discover_types() unions
    several.
    """
    soup = make_soup(html)
    types: dict[int, StarType] = {}
    for a in soup.select('a[href*="/mykp/stars/list/type/"]'):
        tid = _plain_type_id(a.get("href", ""))
        if tid is None or tid in types:
            continue
        name = a.get_text(" ", strip=True)
        if not name:
            continue
        parent_text = a.parent.get_text(" ", strip=True) if a.parent else ""
        cm = COUNT_IN_PARENS_RE.search(parent_text)
        count = int(cm.group(1)) if cm else None
        types[tid] = StarType(id=tid, name=name, count=count)
    return list(types.values())


def discover_types(
        session: requests.Session,
        *,
        seeds: list[int] | None = None,
        pause: float = 2.0,
) -> list[StarType]:
    """Union the type sidebars of several seed pages into the full list."""
    seeds = list(dict.fromkeys(seeds or DEFAULT_SEED_TYPES))
    types: dict[int, StarType] = {}
    errors: list[str] = []
    for i, sid in enumerate(seeds):
        if i:
            time.sleep(pause)
        try:
            html = fetch(session, stars_url(sid, 1), pause=pause)
        except Exception as e:  # noqa: BLE001 — a dead seed shouldn't abort discovery
            errors.append(f"{sid}: {e}")
            continue
        for t in parse_type_sidebar(html):
            cur = types.get(t.id)
            if cur is None or (cur.count is None and t.count is not None):
                types[t.id] = t
    if not types:
        raise RuntimeError(
            "No star types found in sidebars of seeds "
            f"{seeds}. Are you logged in? Errors: {errors or 'none'}"
        )
    return list(types.values())


# --------------------------------------------------------------------------- #
# Stars page parsing
# --------------------------------------------------------------------------- #
def stars_url(type_id: int | str, page: int = 1, perpage: int = STARS_PERPAGE) -> str:
    # Stars use a path-segment style: .../perpage/<n>/page/<n>/
    base = f"{BASE_URL}/mykp/stars/list/type/{type_id}/perpage/{perpage}/"
    return base if page <= 1 else f"{base}page/{page}/"


def parse_person(item, base_url: str = BASE_URL) -> Person:
    """Parse one <li data-id> person row of a stars page into a Person."""
    person = Person()

    data_id = item.get("data-id")
    if data_id and data_id.isdigit():
        person.kp_id = int(data_id)

    name_link = item.select_one("div.info a.name") or item.select_one(
        'div.info a[href*="/name/"]'
    )
    if name_link and name_link.get("href"):
        href = name_link["href"]
        person.url = urljoin(base_url, href)
        m = NAME_ID_RE.search(href)
        if m and person.kp_id is None:
            person.kp_id = int(m.group(1))
        person.name = name_link.get_text(" ", strip=True) or None

    info = item.select_one("div.info")
    if info:
        for span in info.find_all("span", recursive=False):
            if "last" in (span.get("class") or []):
                continue  # the "Фото • Награды" nav row
            text = span.get_text(" ", strip=True)
            if not text:
                continue
            m = BIRTH_DATE_RE.search(text)
            if m:
                person.birth_date = m.group(1)  # "30 сентября 1982"
            elif person.original_name is None and not any(ch.isdigit() for ch in text):
                person.original_name = text  # "Kieran Culkin"

    # photo: real path lives in the img title (src is a spacer gif)
    img = item.select_one("div.pic img") or item.select_one("img")
    if img:
        person.alt = img.get("alt") or person.alt
        title = img.get("title")
        if title and "spacer" not in title:
            if title.startswith("http"):
                person.photo = title
            elif title.startswith("/"):
                person.photo = PHOTO_HOST + title
        # fall back to the "(Latin)" tail of the alt if there was no name span
        if person.original_name is None and person.alt:
            am = re.search(r"\(([^)]+)\)\s*$", person.alt)
            if am:
                person.original_name = am.group(1)

    return person


def parse_stars_page(html: str, base_url: str = BASE_URL) -> tuple[list[Person], int | None]:
    """All people on a stars page + the reported total (from 'X—Y из N')."""
    soup = make_soup(html)
    people = [parse_person(li, base_url) for li in soup.select("ul.itemList li[data-id]")]

    total = None
    from_to = soup.select_one(".pagesFromTo")
    if from_to:
        m = PAGES_FROM_TO_RE.search(from_to.get_text(" ", strip=True))
        if m:
            total = int(m.group(3))
    return people, total


# --------------------------------------------------------------------------- #
# Scraping a type across pages
# --------------------------------------------------------------------------- #
def scrape_type(
        session: requests.Session,
        star_type: StarType,
        *,
        max_pages: int | None = None,
        perpage: int = STARS_PERPAGE,
        pause: float = 2.0,
) -> list[Person]:
    """Collect every person across all pages of one type (deduped by id)."""
    first_html = fetch(session, stars_url(star_type.id, 1, perpage), pause=pause)
    page_people, total = parse_stars_page(first_html)

    people: list[Person] = []
    seen: set[int] = set()

    def add(batch: list[Person]) -> int:
        added = 0
        for p in batch:
            key = p.kp_id if p.kp_id is not None else id(p)
            if key in seen:
                continue
            seen.add(key)
            p.list_type_id = star_type.id
            p.list_type_name = star_type.name
            people.append(p)
            added += 1
        return added

    added = add(page_people)
    print(
        f"type {star_type.id} ({star_type.name}) page 1: {added} people"
        + (f" / {total} total" if total else ""),
        file=sys.stderr,
    )

    page = 2
    while page_people:
        if max_pages and page > max_pages:
            break
        if total is not None and len(people) >= total:
            break
        time.sleep(pause)
        html = fetch(session, stars_url(star_type.id, page, perpage), pause=pause)
        page_people, page_total = parse_stars_page(html)
        if page_total is not None:
            total = page_total
        added = add(page_people)
        if added == 0:
            break  # nothing new (past the end or a repeated page)
        print(
            f"type {star_type.id} ({star_type.name}) page {page}: {added} people "
            f"(collected {len(people)}" + (f"/{total}" if total else "") + ")",
            file=sys.stderr,
        )
        page += 1

    return people


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
def type_out_path(out_dir: Path, star_type: StarType, fmt: str) -> Path:
    ext = "csv" if fmt == "csv" else "json"
    return out_dir / f"{star_type.id}-{slugify(star_type.name)}.{ext}"


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sel = p.add_mutually_exclusive_group(required=True)
    sel.add_argument(
        "--type", action="append", dest="types", metavar="ID",
        help="star-type id to scrape (repeatable)",
    )
    sel.add_argument("--all", action="store_true", help="scrape all discovered types")
    sel.add_argument(
        "--list", action="store_true",
        help="only print discovered types (id, name, count) and exit",
    )

    p.add_argument(
        "--out-dir", default="out/kinopoisk/stars",
        help="directory for per-type files (default: out/kinopoisk/stars/)",
    )
    p.add_argument(
        "--format", choices=["json", "csv"], default="json",
        help="output format (default: json)",
    )
    p.add_argument("--max-pages", type=int, help="limit pages scraped per type")
    p.add_argument(
        "--perpage", type=int, default=STARS_PERPAGE,
        help=f"people per page / request (max 200; default {STARS_PERPAGE})",
    )
    p.add_argument(
        "--seed-type", type=int, action="append", dest="seeds", metavar="ID",
        help=(
            "extra type id to seed discovery from (repeatable); always unioned "
            f"with system types {DEFAULT_SEED_TYPES}"
        ),
    )
    add_http_args(p)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    session = session_from_args(args)

    seeds = (args.seeds or []) + DEFAULT_SEED_TYPES
    try:
        discovered = {t.id: t for t in discover_types(session, seeds=seeds, pause=args.pause)}
    except Exception as e:  # noqa: BLE001 — clear message instead of a traceback
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    if args.types:
        targets = [
            discovered.get(int(tid), StarType(id=int(tid), name=str(tid)))
            for tid in args.types
        ]
    else:
        targets = list(discovered.values())

    if args.list:
        for t in targets:
            print(f"{t.id}\t{t.count if t.count is not None else '?'}\t{t.name}")
        return 0

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    grand_total = 0
    for star_type in targets:
        try:
            people = scrape_type(
                session, star_type, max_pages=args.max_pages,
                perpage=args.perpage, pause=args.pause,
            )
        except Exception as e:  # noqa: BLE001 — one type must not kill the run
            print(f"type {star_type.id} ({star_type.name}): ERROR {e}", file=sys.stderr)
            continue
        path = type_out_path(out_dir, star_type, args.format)
        save_records(people, path, args.format)
        grand_total += len(people)
        print(f"saved {len(people)} people -> {path}", file=sys.stderr)
        time.sleep(args.pause)

    print(f"Done: {grand_total} people across {len(targets)} types -> {out_dir}/", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
