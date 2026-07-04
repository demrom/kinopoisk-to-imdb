"""Add resolved IMDb consts to one of your IMDb lists.

The normal flow is two steps: `python -m imdb.search` resolves your records into
a matches file you can eyeball, then this reads that file and adds each title to
a target list via the AddConstToList GraphQL mutation. This mutates your
account, so it needs your IMDb cookies (pulled from the browser via
fetch_cookies by default). You can also skip the file and resolve a records file
on the fly by passing it positionally.

By default it skips rows with no match and rows flagged for review (a wrong add
is annoying to undo); pass --include-flagged to add them too. Use --dry-run
first to see exactly what would be added — flagged rows are printed with the
reason they look off.

Find your list id in the list URL: imdb.com/list/ls123456789/ -> ls123456789.

Examples:
    # dry run from a reviewed matches file (mirrors the Kinopoisk output tree)
    python -m imdb.lists \\
        --from-matches out/imdb/from-kinopoisk/content-lists/12345678-my-list.json \\
        --list-id ls123456789 --dry-run

    # for real, only movies you rated 7+
    python -m imdb.lists --from-matches matches.json --list-id ls123456789 --min-rating 7

    # resolve a records file and import in one shot
    python -m imdb.lists out/kinopoisk/content-lists/12345678-my-list.json --list-id ls123456789
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from .core import (
    DECISION_NO,
    DECISION_REVIEW,
    LIST_ENV,
    Match,
    add_field_map_args,
    add_to_list,
    field_map_from_args,
    load_matches,
    load_records,
    make_session,
    mark_watched,
    resolve_cookie,
    resolve_records,
)


def _is_duplicate(err: str) -> bool:
    low = err.lower()
    return any(w in low for w in ("already", "duplicate", "exists"))


def add_cookie_args(parser: argparse.ArgumentParser) -> None:
    """Cookie-source flags (mirrors kinopoisk.core.add_http_args)."""
    parser.add_argument("--cookie", help="IMDb Cookie string from the browser")
    parser.add_argument("--cookie-file", help="file with the IMDb Cookie string")
    parser.add_argument(
        "--browser", action="append", dest="browsers", metavar="NAME",
        help="browser to read cookies from (arc/chrome/...); repeatable",
    )
    parser.add_argument(
        "--no-browser-cookie", action="store_true",
        help="do not auto-pull cookies from the browser",
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument(
        "records", nargs="?",
        help="records file to resolve then import (.json or .csv)",
    )
    src.add_argument(
        "--from-matches",
        help="a matches file from `imdb.search` (skip resolving) — the usual path",
    )

    p.add_argument(
        "--list-id",
        help=f"target IMDb list id, e.g. ls123456789 (or ${LIST_ENV})",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="show what would be added without touching IMDb",
    )
    p.add_argument(
        "--mark-watched", action="store_true",
        help="also mark each added title as watched (addWatchedTitle). Rating a "
             "title already marks it watched, so this is for unrated ones.",
    )

    # Which resolved rows to actually add.
    p.add_argument(
        "--include-flagged", "--include-ambiguous", action="store_true",
        dest="include_flagged",
        help="also add rows flagged for review (default: skip them)",
    )
    p.add_argument(
        "--min-rating", type=int, metavar="N",
        help="only add records whose rating field is >= N",
    )
    p.add_argument(
        "--only-positive", action="store_true",
        help="only add records whose sentiment field is 'positive'",
    )
    p.add_argument("--limit", type=int, help="add only the first N eligible items")

    p.add_argument(
        "--report",
        help="write a per-item outcome report (json) here",
    )
    p.add_argument(
        "--pause", type=float, default=1.0,
        help="pause between list mutations, sec",
    )
    add_cookie_args(p)
    add_field_map_args(p)  # only used when resolving a records file on the fly
    return p


def load_or_resolve(args, session) -> list[Match]:
    """Get matches from a matches file, or by resolving a records file."""
    if args.from_matches:
        return load_matches(Path(args.from_matches))
    records = load_records(Path(args.records))
    if not records:
        print(f"ERROR: no records loaded from {args.records}", file=sys.stderr)
        return []
    field_map = field_map_from_args(args)
    print(
        f"Resolving {len(records)} records against IMDb "
        f"(search by: {', '.join(field_map.search)})...",
        file=sys.stderr,
    )
    return resolve_records(session, records, field_map, pause=max(args.pause, 0.3))


def eligible(m: Match, args) -> tuple[bool, str | None]:
    """Should this match be added? Returns (ok, reason-if-skipped).

    Driven by the `decision` column: accept imports, reject/unmatched skip,
    review skips unless --include-flagged. A blank decision falls back to the
    ambiguity flag (for hand-made files without the column). Lists hold titles,
    so a resolved person (`nm…`) is skipped, like imdb.ratings/imdb.watched.
    """
    dec = (m.decision or "").strip().lower()
    if not m.matched:
        return False, "unmatched"
    if not str(m.imdb_const).startswith("tt"):
        return False, "not-a-title"
    if dec in DECISION_NO:
        return False, "rejected"
    if dec == DECISION_REVIEW and not args.include_flagged:
        return False, "review"
    if not dec and m.ambiguous and not args.include_flagged:
        return False, "review"
    if args.min_rating is not None and (m.src_rating or 0) < args.min_rating:
        return False, f"rating<{args.min_rating}"
    if args.only_positive and m.src_sentiment != "positive":
        return False, "not-positive"
    return True, None


def _label(m: Match) -> str:
    """Human line for a match, calling out a title mismatch when present."""
    base = f"{m.imdb_const} {m.imdb_title or m.src_title or ''}".strip()
    if m.src_title and m.imdb_title and m.src_title != m.imdb_title:
        base += f"  (from: {m.src_title})"
    if m.ambiguous and m.review:
        base += f"  [review: {m.review}]"
    return base


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    list_id = args.list_id or os.environ.get(LIST_ENV)
    if not list_id and not args.dry_run:
        print(f"ERROR: no list id (pass --list-id or set ${LIST_ENV})", file=sys.stderr)
        return 2

    # Only resolving a records file (not a matches file) hits the public API; the
    # mutation needs cookies. Build one session that can do both.
    cookie = None
    if not args.dry_run:
        cookie = resolve_cookie(
            args.cookie, args.cookie_file,
            use_browser=not args.no_browser_cookie, browsers=args.browsers,
        )
    session = make_session(cookie=cookie, need_auth=not args.dry_run)

    matches = load_or_resolve(args, session)
    if not matches:
        return 1

    # Split into to-add vs skipped.
    to_add: list[Match] = []
    skipped: dict[str, int] = {}
    for m in matches:
        ok, reason = eligible(m, args)
        if ok:
            to_add.append(m)
        else:
            skipped[reason] = skipped.get(reason, 0) + 1
    if args.limit:
        to_add = to_add[: args.limit]

    skip_note = ", ".join(f"{k}={v}" for k, v in sorted(skipped.items())) or "none"
    if skipped.get("review"):
        skip_note += " (edit their 'decision' to accept, or pass --include-flagged)"
    print(
        f"{len(to_add)} to add, skipped: {skip_note}"
        + (f"  (list {list_id})" if list_id else "")
        + ("  [DRY RUN]" if args.dry_run else ""),
        file=sys.stderr,
    )

    added = dupes = failed = 0
    watched_ok = watched_failed = 0
    report: list[dict] = []
    total = len(to_add)
    for i, m in enumerate(to_add, 1):
        label = _label(m)
        if args.dry_run:
            mark = "?" if m.ambiguous else " "
            extra = " (+ mark watched)" if args.mark_watched else ""
            print(f"[{i}/{total}] {mark} would add {label}{extra}", file=sys.stderr)
            report.append({"const": m.imdb_const, "outcome": "dry-run", "title": m.imdb_title})
            continue
        in_list = False
        try:
            add_to_list(session, list_id, m.imdb_const)
            added += 1
            in_list = True
            outcome = "added"
            print(f"[{i}/{total}] + {label}", file=sys.stderr)
        except Exception as err:  # noqa: BLE001 — one item must not kill the run
            if _is_duplicate(str(err)):
                dupes += 1
                in_list = True
                outcome = "duplicate"
                print(f"[{i}/{total}] = already in list: {label}", file=sys.stderr)
            else:
                failed += 1
                outcome = f"error: {err}"
                print(f"[{i}/{total}] x FAILED {label}: {err}", file=sys.stderr)
                # An auth failure dooms every remaining item — stop early.
                if "Authentication required" in str(err) or "FORBIDDEN" in str(err):
                    print("Aborting: not authenticated (bad/expired cookies).", file=sys.stderr)
                    report.append({"const": m.imdb_const, "outcome": outcome, "title": m.imdb_title})
                    break
        # Optionally mark the title watched too. Best-effort: the add already
        # counted, so a watched hiccup notes but doesn't fail the item.
        if args.mark_watched and in_list:
            try:
                res = mark_watched(session, m.imdb_const)
                # A missing/false `success` (incl. a null result) is a failure.
                if not res.get("success"):
                    raise RuntimeError(
                        (res.get("message") or {}).get("value") or "not successful"
                    )
                watched_ok += 1
                outcome += "+watched"
                print(f"          ↳ watched", file=sys.stderr)
            except Exception as werr:  # noqa: BLE001 — watched is a best-effort add-on
                watched_failed += 1
                print(f"          ↳ watched failed: {werr}", file=sys.stderr)
        report.append({"const": m.imdb_const, "outcome": outcome, "title": m.imdb_title})
        if i < total:
            time.sleep(args.pause)

    if args.report:
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    if args.dry_run:
        note = " (+ mark watched)" if args.mark_watched else ""
        print(f"Dry run: {total} would be added{note}.", file=sys.stderr)
    else:
        line = (
            f"Done: {added} added, {dupes} already there, {failed} failed "
            f"(of {total})."
        )
        if args.mark_watched:
            line += f" Watched: {watched_ok} marked, {watched_failed} failed."
        print(line, file=sys.stderr)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
