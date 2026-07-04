"""Resolve a list of records into IMDb consts (tt...).

Reads a JSON (or CSV) list of records — a Kinopoisk export works out of the box,
but so does any JSON as long as you say which keys to search by — looks each one
up on IMDb's public suggestion API, and writes a matches file (source fields +
resolved const + confidence score + close runners-up).

Output is JSON by default (like the Kinopoisk export), with `decision` and
`review` as the first fields of every row. Each row's `decision` is auto-filled:
`accept` for clean hits, `review` when something looks off (e.g. the title
didn't really match), or `unmatched` when nothing was found. The rows needing a
call (review/unmatched) are sorted to the **top** of the file, so you edit
`decision` in place (review/unmatched -> accept or reject) and hand the same file
to `imdb.lists`, which imports only the `accept` rows — nothing to reconcile.
(Pass --review-file for a separate filtered copy of just those rows, or --format
csv to edit in a spreadsheet — JSON keeps more detail.)

No login needed — the suggestion API is public.

Output convention: the results are derived from a Kinopoisk export, so mirror the
Kinopoisk output tree under out/imdb/from-kinopoisk/ (the id-slug name is
Kinopoisk's, not IMDb's — the path makes the origin obvious):
    out/kinopoisk/content-lists/12345678-my-list.json
        -> out/imdb/from-kinopoisk/content-lists/12345678-my-list.json

Examples:
    # a Kinopoisk content-list export (default map: original_title, title, year, kind)
    python -m imdb.search out/kinopoisk/content-lists/12345678-my-list.json \\
        -o out/imdb/from-kinopoisk/content-lists/12345678-my-list.json

    # an arbitrary JSON list: search by these keys, disambiguate by "releaseYear"
    python -m imdb.search items.json --search-fields name,localName --year-field releaseYear

    # records that already carry an IMDb id under "imdb_id": trust it, skip search
    python -m imdb.search items.json --const-field imdb_id

    # people instead of titles: resolve a Kinopoisk stars export to nm... consts
    python -m imdb.search out/kinopoisk/stars/2-актёры.json --entity person \\
        -o out/imdb/from-kinopoisk/stars/2-актёры.json

    # ad-hoc: just search one title (or a person, with --entity person)
    python -m imdb.search --query "The Batman"
    python -m imdb.search --query "Leonardo DiCaprio" --entity person
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .core import (
    add_field_map_args,
    field_map_from_args,
    load_records,
    make_session,
    resolve_records,
    save_matches,
    search_people,
    search_titles,
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "records",
        nargs="?",
        help="records to resolve (.json list or .csv)",
    )
    p.add_argument("-o", "--out", default="matches.json", help="where to write matches")
    p.add_argument(
        "--format",
        choices=["json", "csv"],
        help="output format (default: JSON, inferred from --out). JSON keeps full "
             "detail (alternatives, reasons); CSV is a spreadsheet convenience.",
    )
    p.add_argument(
        "--review-file", action="store_true",
        help="also write a separate worklist of just the rows needing a decision "
             "(off by default — they're already sorted to the top of --out)",
    )
    p.add_argument(
        "--review-out",
        help="path for that worklist (implies --review-file; "
             "default: <out>.review.<ext>)",
    )
    p.add_argument("--limit", type=int, help="resolve only the first N records")
    p.add_argument(
        "--pause", type=float, default=0.5, help="pause between requests, sec"
    )
    p.add_argument(
        "--query",
        help="debug: search this text and print the hits instead of resolving a file",
    )
    add_field_map_args(p)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    session = make_session()

    # Debug mode: one ad-hoc search, print hits, done.
    if args.query:
        finder = search_people if args.entity == "person" else search_titles
        for t in finder(session, args.query):
            year = f" ({t.year})" if t.year else ""
            cat = f"  {t.category}" if t.category else ""
            print(f"{t.const}  {t.title}{year}{cat}  — {t.stars or ''}")
        return 0

    if not args.records:
        print("ERROR: pass a records file, or --query for a one-off search", file=sys.stderr)
        return 2

    records = load_records(Path(args.records))
    if args.limit:
        records = records[: args.limit]
    if not records:
        print(f"ERROR: no records loaded from {args.records}", file=sys.stderr)
        return 1

    field_map = field_map_from_args(args)
    print(
        f"Resolving {len(records)} records against IMDb "
        f"(search by: {', '.join(field_map.search)})...",
        file=sys.stderr,
    )
    matches = resolve_records(session, records, field_map, pause=args.pause)

    # Sort the rows that need a human call (review/unmatched) to the top, so the
    # file opens review-ready — no separate worklist to reconcile. Stable, so the
    # original order is preserved within each group.
    matches.sort(key=lambda m: 0 if (m.decision or "") != "accept" else 1)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    save_matches(matches, out_path, args.format)

    needs = [m for m in matches if (m.decision or "") != "accept"]
    resolved = sum(1 for m in matches if m.matched)
    review_n = sum(1 for m in matches if (m.decision or "") == "review")
    unmatched_n = sum(1 for m in matches if (m.decision or "") == "unmatched")

    # A separate worklist is opt-in now (--review-file / --review-out); the rows
    # are already grouped at the top of the main file.
    review_path = None
    if needs and (args.review_file or args.review_out):
        if args.review_out:
            review_path = Path(args.review_out)
        else:
            review_path = out_path.with_name(
                out_path.stem + ".review" + out_path.suffix
            )
        review_path.parent.mkdir(parents=True, exist_ok=True)
        save_matches(needs, review_path, args.format)

    print(
        f"Done: {resolved}/{len(matches)} resolved "
        f"(accept={resolved - review_n}, review={review_n}, unmatched={unmatched_n}) "
        f"-> {out_path}",
        file=sys.stderr,
    )
    if needs:
        where = f" -> {review_path}" if review_path else " (sorted to the top of the file)"
        print(
            f"{len(needs)} need a decision{where}\n"
            "  edit the 'decision' field (review/unmatched -> accept or reject), "
            "then: python -m imdb.lists --from-matches <file> --list-id ls...",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
