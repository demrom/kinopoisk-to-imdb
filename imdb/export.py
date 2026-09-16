"""Export one of your IMDb lists to JSON/CSV — with your ratings and watched marks.

The other imdb.* steps push data *into* your account; this one reads it back
out. It pulls a list through the same GraphQL endpoint the list page uses, so
each row carries both the title's own facts (year, type, IMDb rating, runtime,
genres, directors) and *your* data on it: the rating you gave, when you gave it,
whether it is marked watched and when you first watched it.

Private lists are only visible to their owner, so this needs your IMDb cookies
(pulled from the browser via fetch_cookies, like the other steps).

Rows use the same key names as a Kinopoisk export (`title`, `year`, `kind`,
`user_rating`, `list_id`, `list_name`, `list_position`, `added_at`), so an
exported list feeds straight back into the rest of the tool, e.g.
`python -m imdb.search <file> --const-field const`.

Examples:
    # which lists do I have?
    python -m imdb.export --list

    # both lists -> out/imdb/lists/<ls-id>-<slug>.json
    python -m imdb.export ls123456789 ls987654321

    # a spreadsheet instead
    python -m imdb.export ls123456789 --format csv

    # somewhere specific (single list only)
    python -m imdb.export ls123456789 -o ~/Desktop/watchlist.csv

    # only what you have actually watched
    python -m imdb.export ls123456789 --watched-only
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .core import (
    LIST_PAGE_SIZE,
    add_cookie_args,
    fetch_list_entries,
    fetch_user_lists,
    make_session,
    resolve_cookie,
    save_rows,
    slugify,
)

DEFAULT_OUT_DIR = Path("out/imdb/lists")

# Column order for CSV: identity first, then your own data (the point of the
# export), then the title's public facts.
CSV_ORDER = [
    "list_position", "const", "title", "original_title", "year", "end_year",
    "kind", "user_rating", "user_rated_at", "watched", "watched_at",
    "imdb_rating", "num_votes", "genres", "runtime_min", "release_date",
    "directors", "url", "added_at", "note", "item_id", "list_id", "list_name",
]


def _text(node: dict | None, *keys: str):
    """Walk a chain of optional dict keys: _text(n, 'titleText', 'text')."""
    cur = node
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _date(release: dict | None) -> str | None:
    """{day, month, year} -> 'YYYY-MM-DD' (or just what is known)."""
    if not release or not release.get("year"):
        return None
    y, m, d = release.get("year"), release.get("month"), release.get("day")
    if m and d:
        return f"{y:04d}-{m:02d}-{d:02d}"
    if m:
        return f"{y:04d}-{m:02d}"
    return f"{y:04d}"


def build_row(node: dict, list_meta: dict, position: int) -> dict:
    """Flatten one list item (+ its title) into an export row."""
    item = node.get("item") or {}
    const = item.get("id")
    rating = item.get("userRating") or {}
    watched = item.get("userWatchedStatus") or {}
    genres = [
        _text(g, "genre", "text")
        for g in _text(item, "titleGenres", "genres") or []
    ]
    directors = [
        _text(c, "name", "nameText", "text")
        for credit in item.get("principalCredits") or []
        for c in credit.get("credits") or []
    ]
    runtime = _text(item, "runtime", "seconds")
    is_title = item.get("__typename") == "Title"
    return {
        "list_position": position,
        "const": const,
        # a list can also hold people/images; keep them, flagged by `kind`
        "title": _text(item, "titleText", "text") or _text(item, "nameText", "text"),
        "original_title": _text(item, "originalTitleText", "text"),
        "year": _text(item, "releaseYear", "year"),
        "end_year": _text(item, "releaseYear", "endYear"),
        "kind": _text(item, "titleType", "id") if is_title
                else "person" if item.get("__typename") == "Name"
                else (item.get("__typename") or "").lower() or None,
        "user_rating": rating.get("value"),
        "user_rated_at": rating.get("date"),
        "watched": bool(watched.get("isWatched")),
        "watched_at": watched.get("firstWatched"),
        "imdb_rating": _text(item, "ratingsSummary", "aggregateRating"),
        "num_votes": _text(item, "ratingsSummary", "voteCount"),
        "genres": [g for g in genres if g],
        "runtime_min": round(runtime / 60) if runtime else None,
        "release_date": _date(item.get("releaseDate")),
        "directors": [d for d in directors if d],
        "url": f"https://www.imdb.com/{'title' if is_title else 'name'}/{const}/"
               if const else None,
        "added_at": node.get("createdDate"),
        "note": _text(node, "description", "originalText", "plainText"),
        # the handle on *this* entry: a list can hold the same title twice, and
        # then the const no longer identifies a row
        "item_id": node.get("itemId"),
        "list_id": list_meta["id"],
        "list_name": list_meta["name"],
    }


def fetch_list(session, list_id: str, *, pause: float = 0.5) -> tuple[dict, list[dict]]:
    """Pull a whole list. Returns (metadata, rows in list order)."""
    payload, nodes = fetch_list_entries(
        session, list_id, pause=pause,
        progress=lambda got, total: print(f"  {list_id}: {got}/{total} items", file=sys.stderr),
    )
    meta = {
        "id": payload.get("id") or list_id,
        "name": _text(payload, "name", "originalText") or list_id,
        "description": _text(payload, "description", "originalText", "plainText"),
        "created_at": payload.get("createdDate"),
        "modified_at": payload.get("lastModifiedDate"),
        "visibility": _text(payload, "visibility", "id"),
        "total": payload.get("total"),
    }
    return meta, [build_row(node, meta, i) for i, node in enumerate(nodes, 1)]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "list_ids", nargs="*", metavar="ls123456789",
        help="IMDb list id(s), from the list URL: imdb.com/list/ls123456789/",
    )
    p.add_argument(
        "--list", action="store_true",
        help="print the catalog of your lists (id, size, type, name) and exit",
    )
    p.add_argument(
        "-o", "--out", metavar="PATH",
        help="write here (single list only); the suffix picks the format",
    )
    p.add_argument(
        "--out-dir", default=str(DEFAULT_OUT_DIR), metavar="DIR",
        help=f"directory for <ls-id>-<slug>.<ext> files (default: {DEFAULT_OUT_DIR})",
    )
    p.add_argument(
        "--format", choices=["json", "csv"], default="json",
        help="output format (default: json)",
    )
    p.add_argument(
        "--watched-only", action="store_true",
        help="keep only items you have marked watched",
    )
    p.add_argument(
        "--rated-only", action="store_true",
        help="keep only items you have rated",
    )
    p.add_argument(
        "--pause", type=float, default=0.5,
        help=f"pause between pages of {LIST_PAGE_SIZE} items, sec",
    )
    add_cookie_args(p)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.list_ids and not args.list:
        print("ERROR: pass a list id, or --list to see the catalog", file=sys.stderr)
        return 2
    if args.out and len(args.list_ids) > 1:
        print("ERROR: -o takes a single list id; use --out-dir", file=sys.stderr)
        return 2

    cookie = resolve_cookie(
        args.cookie, args.cookie_file,
        use_browser=not args.no_browser_cookie, browsers=args.browsers,
    )
    session = make_session(cookie=cookie, need_auth=True)

    if args.list:
        lists = fetch_user_lists(session)
        for lst in lists:
            print(f"{lst['id']:<14} {lst['total'] or 0:>6} items  {lst['type'] or '-':<7} {lst['name']}")
        print(f"{len(lists)} lists", file=sys.stderr)
        return 0

    failed = 0
    for list_id in args.list_ids:
        try:
            meta, rows = fetch_list(session, list_id, pause=args.pause)
        except Exception as e:  # noqa: BLE001 — one bad id shouldn't stop the rest
            print(f"ERROR: {list_id}: {e}", file=sys.stderr)
            failed += 1
            continue

        if args.watched_only:
            rows = [r for r in rows if r["watched"]]
        if args.rated_only:
            rows = [r for r in rows if r["user_rating"] is not None]

        if args.out:
            path = Path(args.out)
            fmt = None  # let the suffix decide
        else:
            ext = args.format
            path = Path(args.out_dir) / f"{meta['id']}-{slugify(meta['name'])}.{ext}"
            fmt = args.format
        save_rows(rows, path, fmt, field_order=CSV_ORDER)

        watched = sum(1 for r in rows if r["watched"])
        rated = sum(1 for r in rows if r["user_rating"] is not None)
        print(
            f"{meta['id']} {meta['name']!r}: {len(rows)} items "
            f"({watched} watched, {rated} rated) -> {path}",
            file=sys.stderr,
        )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
