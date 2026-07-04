"""Transfer your ratings to IMDb — set your personal rating on each title.

Sibling of imdb.lists: where that adds titles to a list, this sets your personal
IMDb rating on each resolved title, via the rateTitle GraphQL mutation. Kinopoisk
and IMDb both rate on a 1..10 scale, so the rating carries over as-is. This
mutates your account, so it needs your IMDb cookies (pulled from the browser via
fetch_cookies by default). You can also skip the file and resolve a records file
on the fly by passing it positionally.

The normal flow is two steps, like lists:

    # 1. resolve your rated movies to IMDb ids
    python -m imdb.search out/kinopoisk/ratings.json \\
        -o out/imdb/from-kinopoisk/ratings.json

    # 2. review the matches, then rate the accept rows (dry run first!)
    python -m imdb.ratings --from-matches out/imdb/from-kinopoisk/ratings.json --dry-run
    python -m imdb.ratings --from-matches out/imdb/from-kinopoisk/ratings.json

Ratings from a Kinopoisk export tend to resolve to `review` (the export carries
no original title to match on), so eyeball the file and set `decision` to
`accept` before rating — a wrong rating has to be undone one at a time.

By default it skips rows with no match, no rating, or flagged for review; pass
--include-flagged to rate review rows too. Filters: --min-rating, --only-positive,
--limit. Use --delete to CLEAR the rating each row would set instead (undo), via
deleteTitleRating.

Examples:
    # for real, only movies you rated 7+
    python -m imdb.ratings --from-matches matches.json --min-rating 7

    # undo: clear the ratings this file set
    python -m imdb.ratings --from-matches matches.json --delete
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .core import (
    DECISION_NO,
    DECISION_REVIEW,
    Match,
    add_field_map_args,
    delete_rating,
    field_map_from_args,
    load_matches,
    load_records,
    make_session,
    rate_title,
    resolve_cookie,
    resolve_records,
)


def add_cookie_args(parser: argparse.ArgumentParser) -> None:
    """Cookie-source flags (mirrors imdb.lists.add_cookie_args)."""
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
        help="records file to resolve then rate (.json or .csv)",
    )
    src.add_argument(
        "--from-matches",
        help="a matches file from `imdb.search` (skip resolving) — the usual path",
    )

    p.add_argument(
        "--dry-run", action="store_true",
        help="show what would be rated without touching IMDb",
    )
    p.add_argument(
        "--delete", action="store_true",
        help="CLEAR the rating on each eligible title instead of setting it (undo)",
    )

    # Which resolved rows to actually rate.
    p.add_argument(
        "--include-flagged", "--include-ambiguous", action="store_true",
        dest="include_flagged",
        help="also rate rows flagged for review (default: skip them)",
    )
    p.add_argument(
        "--min-rating", type=int, metavar="N",
        help="only rate records whose rating is >= N",
    )
    p.add_argument(
        "--only-positive", action="store_true",
        help="only rate records whose sentiment field is 'positive'",
    )
    p.add_argument("--limit", type=int, help="rate only the first N eligible items")

    p.add_argument("--report", help="write a per-item outcome report (json) here")
    p.add_argument(
        "--pause", type=float, default=1.0,
        help="pause between rating mutations, sec",
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


def _rating_value(m: Match) -> int | None:
    """The 1..10 rating to set, or None if the row carries no usable rating."""
    r = m.src_rating
    if r is None:
        return None
    try:
        r = int(r)
    except (TypeError, ValueError):
        return None
    return r if 1 <= r <= 10 else None


def eligible(m: Match, args) -> tuple[bool, str | None]:
    """Should this match be rated? Returns (ok, reason-if-skipped).

    Driven by the `decision` column, like imdb.lists: accept rates, reject/
    unmatched skip, review skips unless --include-flagged. Ratings apply to
    titles only (tt...), and the row must carry a 1..10 rating — `--delete` only
    clears ratings this file would set, so it skips no-rating rows too (undoing
    nothing there, and never touching a rating the user set some other way).
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
    if _rating_value(m) is None:
        return False, "no-rating"
    if args.min_rating is not None and (m.src_rating or 0) < args.min_rating:
        return False, f"rating<{args.min_rating}"
    if args.only_positive and m.src_sentiment != "positive":
        return False, "not-positive"
    return True, None


def _label(m: Match, args) -> str:
    """Human line for a match, showing the rating and any title mismatch."""
    base = f"{m.imdb_const} {m.imdb_title or m.src_title or ''}".strip()
    r = _rating_value(m)
    if r is not None and not args.delete:
        base += f"  ({r}/10)"
    if m.src_title and m.imdb_title and m.src_title != m.imdb_title:
        base += f"  (from: {m.src_title})"
    if m.ambiguous and m.review:
        base += f"  [review: {m.review}]"
    return base


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

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

    # Split into to-rate vs skipped.
    to_rate: list[Match] = []
    skipped: dict[str, int] = {}
    for m in matches:
        ok, reason = eligible(m, args)
        if ok:
            to_rate.append(m)
        else:
            skipped[reason] = skipped.get(reason, 0) + 1
    if args.limit:
        to_rate = to_rate[: args.limit]

    verb = "clear" if args.delete else "rate"
    skip_note = ", ".join(f"{k}={v}" for k, v in sorted(skipped.items())) or "none"
    if skipped.get("review"):
        skip_note += " (edit their 'decision' to accept, or pass --include-flagged)"
    print(
        f"{len(to_rate)} to {verb}, skipped: {skip_note}"
        + ("  [DRY RUN]" if args.dry_run else ""),
        file=sys.stderr,
    )

    done = failed = 0
    report: list[dict] = []
    total = len(to_rate)
    for i, m in enumerate(to_rate, 1):
        label = _label(m, args)
        rating = _rating_value(m)
        if args.dry_run:
            mark = "?" if m.ambiguous else " "
            print(f"[{i}/{total}] {mark} would {verb} {label}", file=sys.stderr)
            report.append({"const": m.imdb_const, "outcome": "dry-run",
                           "rating": None if args.delete else rating})
            continue
        try:
            if args.delete:
                delete_rating(session, m.imdb_const)
                outcome = "cleared"
            else:
                rate_title(session, m.imdb_const, rating)
                outcome = f"rated {rating}"
            done += 1
            print(f"[{i}/{total}] + {outcome}: {label}", file=sys.stderr)
        except Exception as err:  # noqa: BLE001 — one item must not kill the run
            failed += 1
            outcome = f"error: {err}"
            print(f"[{i}/{total}] x FAILED {label}: {err}", file=sys.stderr)
            # An auth failure dooms every remaining item — stop early.
            if "Authentication required" in str(err) or "FORBIDDEN" in str(err):
                print("Aborting: not authenticated (bad/expired cookies).", file=sys.stderr)
                report.append({"const": m.imdb_const, "outcome": outcome,
                               "rating": None if args.delete else rating})
                break
        report.append({"const": m.imdb_const, "outcome": outcome,
                       "rating": None if args.delete else rating})
        if i < total:
            time.sleep(args.pause)

    if args.report:
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    if args.dry_run:
        print(f"Dry run: {total} would be {verb}d.", file=sys.stderr)
    else:
        past = "cleared" if args.delete else "rated"
        print(f"Done: {done} {past}, {failed} failed (of {total}).", file=sys.stderr)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
