"""Mark titles as watched on IMDb (or unmark them).

Sibling of imdb.lists / imdb.ratings: reads a matches file from `imdb.search`
(or resolves a records file on the fly) and marks each resolved title as watched
via the addWatchedTitle mutation. This mutates your account, so it needs your
IMDb cookies (pulled from the browser via fetch_cookies by default).

Note: rating a title already marks it watched, so if you ran `imdb.ratings` those
are already covered — this is mainly for titles you watched but didn't rate, e.g.
a Kinopoisk "watched" content list. --unwatch does the reverse (removeWatchedTitle).

The normal flow:

    # resolve a Kinopoisk "watched" content list to IMDb ids
    python -m imdb.search out/kinopoisk/content-lists/112-....json -o matches.json

    # dry run, then mark watched for real
    python -m imdb.watched --from-matches matches.json --dry-run
    python -m imdb.watched --from-matches matches.json

By default it marks rows with a match and decision=accept; --include-flagged adds
review rows. Filters: --min-rating, --only-positive, --limit. --unwatch removes
the watched mark instead.

Examples:
    # only mark ones you rated 7+
    python -m imdb.watched --from-matches matches.json --min-rating 7

    # undo: remove the watched mark these rows set
    python -m imdb.watched --from-matches matches.json --unwatch
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
    field_map_from_args,
    load_matches,
    load_records,
    make_session,
    mark_watched,
    resolve_cookie,
    resolve_records,
    unmark_watched,
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
        help="records file to resolve then mark (.json or .csv)",
    )
    src.add_argument(
        "--from-matches",
        help="a matches file from `imdb.search` (skip resolving) — the usual path",
    )

    p.add_argument(
        "--dry-run", action="store_true",
        help="show what would be marked without touching IMDb",
    )
    p.add_argument(
        "--unwatch", action="store_true",
        help="REMOVE the watched mark on each eligible title instead of adding it",
    )

    # Which resolved rows to actually mark.
    p.add_argument(
        "--include-flagged", "--include-ambiguous", action="store_true",
        dest="include_flagged",
        help="also mark rows flagged for review (default: skip them)",
    )
    p.add_argument(
        "--min-rating", type=int, metavar="N",
        help="only mark records whose rating is >= N",
    )
    p.add_argument(
        "--only-positive", action="store_true",
        help="only mark records whose sentiment field is 'positive'",
    )
    p.add_argument("--limit", type=int, help="mark only the first N eligible items")

    p.add_argument("--report", help="write a per-item outcome report (json) here")
    p.add_argument(
        "--pause", type=float, default=1.0,
        help="pause between watched mutations, sec",
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
    """Should this match be marked? Returns (ok, reason-if-skipped).

    Driven by the `decision` column, like imdb.lists/imdb.ratings: accept marks,
    reject/unmatched skip, review skips unless --include-flagged. Watched applies
    to titles only (tt...).
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

    # Split into to-mark vs skipped.
    to_mark: list[Match] = []
    skipped: dict[str, int] = {}
    for m in matches:
        ok, reason = eligible(m, args)
        if ok:
            to_mark.append(m)
        else:
            skipped[reason] = skipped.get(reason, 0) + 1
    if args.limit:
        to_mark = to_mark[: args.limit]

    verb = "unmark" if args.unwatch else "mark"
    action = unmark_watched if args.unwatch else mark_watched
    skip_note = ", ".join(f"{k}={v}" for k, v in sorted(skipped.items())) or "none"
    if skipped.get("review"):
        skip_note += " (edit their 'decision' to accept, or pass --include-flagged)"
    print(
        f"{len(to_mark)} to {verb} watched, skipped: {skip_note}"
        + ("  [DRY RUN]" if args.dry_run else ""),
        file=sys.stderr,
    )

    done = failed = 0
    report: list[dict] = []
    total = len(to_mark)
    for i, m in enumerate(to_mark, 1):
        label = _label(m)
        if args.dry_run:
            mark = "?" if m.ambiguous else " "
            print(f"[{i}/{total}] {mark} would {verb} watched {label}", file=sys.stderr)
            report.append({"const": m.imdb_const, "outcome": "dry-run"})
            continue
        try:
            res = action(session, m.imdb_const)
            # These mutations report success in the body rather than raising.
            if res.get("success") is False:
                msg = ((res.get("message") or {}).get("value")) or "not successful"
                failed += 1
                outcome = f"error: {msg}"
                print(f"[{i}/{total}] x FAILED {label}: {msg}", file=sys.stderr)
            else:
                done += 1
                outcome = "unwatched" if args.unwatch else "watched"
                print(f"[{i}/{total}] + {outcome}: {label}", file=sys.stderr)
        except Exception as err:  # noqa: BLE001 — one item must not kill the run
            failed += 1
            outcome = f"error: {err}"
            print(f"[{i}/{total}] x FAILED {label}: {err}", file=sys.stderr)
            # An auth failure dooms every remaining item — stop early.
            if "Authentication required" in str(err) or "FORBIDDEN" in str(err):
                print("Aborting: not authenticated (bad/expired cookies).", file=sys.stderr)
                report.append({"const": m.imdb_const, "outcome": outcome})
                break
        report.append({"const": m.imdb_const, "outcome": outcome})
        if i < total:
            time.sleep(args.pause)

    if args.report:
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    if args.dry_run:
        print(f"Dry run: {total} would be {verb}ed watched.", file=sys.stderr)
    else:
        past = "unwatched" if args.unwatch else "watched"
        print(f"Done: {done} {past}, {failed} failed (of {total}).", file=sys.stderr)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
