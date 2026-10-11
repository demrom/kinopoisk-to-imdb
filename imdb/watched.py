"""Mark titles as watched on IMDb (or unmark them).

Sibling of imdb.lists / imdb.ratings: reads a matches file from `imdb.search`
(or resolves a records file on the fly) and marks each resolved title as watched
via the addWatchedTitle mutation. This mutates your account, so it needs your
IMDb cookies (pulled from the browser via fetch_cookies by default).

Note: rating a title already marks it watched, so if you ran `imdb.ratings` those
are already covered — this is mainly for titles you watched but didn't rate, e.g.
a Kinopoisk "watched" content list.

The normal flow:

    # resolve a Kinopoisk "watched" content list to IMDb ids
    python -m imdb.search out/kinopoisk/content-lists/112-....json -o matches.json

    # dry run, then mark watched for real
    python -m imdb.watched --from-matches matches.json --dry-run
    python -m imdb.watched --from-matches matches.json

By default it marks rows with a match and decision=accept; --include-flagged adds
review rows. Filters: --min-rating, --only-positive, --limit.

Titles already marked watched are left alone. Every real run writes a report
(default: out/imdb/reports/watched-<time>.json) listing each title and whether
it was watched before, and --undo <report> removes only the marks the run
added, skipping any title you have rated since (a rating needs the mark).

Examples:
    # only mark ones you rated 7+
    python -m imdb.watched --from-matches matches.json --min-rating 7

    # undo that run
    python -m imdb.watched --undo out/imdb/reports/watched-20261011-093000.json
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
    add_cookie_args,
    add_field_map_args,
    fetch_user_state,
    field_map_from_args,
    load_matches,
    load_records,
    make_session,
    mark_watched,
    resolve_cookie,
    resolve_records,
    unmark_watched,
    write_report,
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
    src.add_argument(
        "--undo", metavar="REPORT",
        help="remove the watched marks a previous run's report says it added",
    )

    p.add_argument(
        "--dry-run", action="store_true",
        help="show what would change without touching IMDb",
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

    p.add_argument(
        "--report",
        help="per-item report path (default for a real run: out/imdb/reports/watched-<time>.json)",
    )
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


def _is_auth_error(msg: str) -> bool:
    return "Authentication required" in msg or "FORBIDDEN" in msg


def _open_report(rows: list[dict], args, kind: str) -> Path | None:
    """Write the report before anything changes, so a bad path stops the run
    early. None for a dry run without --report."""
    if not args.report and args.dry_run:
        return None
    return write_report(rows, args.report, kind)


def apply_marks(args, session, todo: list[tuple[dict, str]], action, done_outcome: str,
                verb: str, save) -> int:
    """Run `action` (mark/unmark) over (report row, label) pairs, recording each
    outcome in its row; `save()` puts the report on disk. Returns the number failed."""
    failed = 0
    total = len(todo)
    for i, (row, label) in enumerate(todo, 1):
        if args.dry_run:
            print(f"[{i}/{total}]   would {verb} {label}", file=sys.stderr)
            continue
        row["outcome"] = "attempted"  # on disk before the request: undo must see it
        save()
        try:
            res = action(session, row["const"])
            # These mutations report success in the body rather than raising, so
            # a missing/false `success` (incl. a null result) is a failure.
            if not res.get("success"):
                raise RuntimeError(((res.get("message") or {}).get("value")) or "not successful")
            row["outcome"] = done_outcome
            print(f"[{i}/{total}] + {done_outcome}: {label}", file=sys.stderr)
        except Exception as err:  # noqa: BLE001 — one item must not kill the run
            failed += 1
            row["outcome"] = f"error: {err}"
            print(f"[{i}/{total}] x FAILED {label}: {err}", file=sys.stderr)
            if _is_auth_error(str(err)):  # dooms every remaining item
                print("Aborting: not authenticated (bad/expired cookies).", file=sys.stderr)
                break
        if i < total:
            time.sleep(args.pause)
    return failed


def _undoable(row: dict) -> bool:
    """Marks a run may have added: done, cut short mid-request, or failed (a
    failed mark can still have landed). "planned" was never sent."""
    outcome = row.get("outcome") or ""
    return outcome in ("watched", "attempted") or outcome.startswith("error")


def run_undo(args, session) -> int:
    """Remove the marks a report says this tool added, unless rated since."""
    if args.report and Path(args.report).resolve() == Path(args.undo).resolve():
        print("ERROR: --report would overwrite the report being undone", file=sys.stderr)
        return 2
    rows = json.loads(Path(args.undo).read_text(encoding="utf-8"))
    if any("was_watched" not in r for r in rows):
        print(f"ERROR: {args.undo} doesn't say which marks were yours before "
              "(written by an older version?); nothing changed", file=sys.stderr)
        return 2
    consts = [r["const"] for r in rows if _undoable(r)]
    if args.limit is not None:
        consts = consts[: args.limit]
    state = fetch_user_state(session, consts)
    todo: list[tuple[dict, str]] = []
    report: list[dict] = []
    failed = 0
    for const in consts:
        cur = state.get(const)
        if cur is None:
            failed += 1
            reason = "unknown"
        elif not cur["watched"]:
            reason = "unchanged"  # already unmarked, or the mark never landed
        elif cur["rating"] is not None:
            reason = "kept"  # rated since: the rating needs the mark
        else:
            row = {"const": const, "outcome": "dry-run" if args.dry_run else "planned"}
            todo.append((row, const))
            report.append(row)
            continue
        print(f"  = {reason}: {const}", file=sys.stderr)
        report.append({"const": const, "outcome": reason})
    print(f"{len(todo)} watched mark(s) to remove"
          + ("  [DRY RUN]" if args.dry_run else ""), file=sys.stderr)
    path = _open_report(report, args, "watched-undo")
    try:
        failed += apply_marks(args, session, todo, unmark_watched, "unwatched", "unmark watched",
                              lambda: path and write_report(report, str(path), "watched-undo"))
    finally:
        if path:
            write_report(report, str(path), "watched-undo")
            print(f"Report: {path}", file=sys.stderr)
    return 0 if failed == 0 else 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.limit is not None and args.limit < 0:
        print("ERROR: --limit must be 0 or more", file=sys.stderr)
        return 2

    # Every path reads your current marks first, so even a dry run needs cookies.
    cookie = resolve_cookie(
        args.cookie, args.cookie_file,
        use_browser=not args.no_browser_cookie, browsers=args.browsers,
    )
    session = make_session(cookie=cookie, need_auth=True)

    try:
        if args.undo:
            return run_undo(args, session)
        return run_mark(args, session)
    except OSError as err:  # mostly: the report can't be written
        print(f"ERROR: {err}", file=sys.stderr)
        return 2


def run_mark(args, session) -> int:
    matches = load_or_resolve(args, session)
    if not matches:
        return 1

    # Split into to-mark vs skipped.
    to_mark: list[Match] = []
    seen: set[str] = set()
    skipped: dict[str, int] = {}
    for m in matches:
        ok, reason = eligible(m, args)
        if ok and m.imdb_const in seen:
            ok, reason = False, "repeated-in-file"
        if ok:
            to_mark.append(m)
            seen.add(m.imdb_const)
        else:
            skipped[reason] = skipped.get(reason, 0) + 1
    if args.limit is not None:  # --limit 0 must mark nothing, not everything
        to_mark = to_mark[: args.limit]

    # Already watched (by you, or by a rating) is recorded as yours, so --undo
    # never removes a mark this run didn't add.
    state = fetch_user_state(session, [m.imdb_const for m in to_mark])
    todo: list[tuple[dict, str]] = []
    report: list[dict] = []
    failed = 0
    for m in to_mark:
        cur = state.get(m.imdb_const)
        if cur is None:
            failed += 1
            report.append({"const": m.imdb_const, "was_watched": None, "outcome": "unknown"})
            print(f"  ? IMDb returned no state: {_label(m)}", file=sys.stderr)
        elif cur["watched"]:
            skipped["already-watched"] = skipped.get("already-watched", 0) + 1
            report.append({"const": m.imdb_const, "was_watched": True, "outcome": "unchanged"})
        else:
            row = {"const": m.imdb_const, "was_watched": False,
                   "outcome": "dry-run" if args.dry_run else "planned"}
            todo.append((row, _label(m)))
            report.append(row)

    skip_note = ", ".join(f"{k}={v}" for k, v in sorted(skipped.items())) or "none"
    if skipped.get("review"):
        skip_note += " (edit their 'decision' to accept, or pass --include-flagged)"
    print(
        f"{len(todo)} to mark watched, skipped: {skip_note}"
        + ("  [DRY RUN]" if args.dry_run else ""),
        file=sys.stderr,
    )
    path = _open_report(report, args, "watched")
    try:
        failed += apply_marks(args, session, todo, mark_watched, "watched", "mark watched",
                              lambda: path and write_report(report, str(path), "watched"))
    finally:
        if path:  # again, with the outcomes; also on Ctrl-C or a crash mid-run
            write_report(report, str(path), "watched")
            print(f"Report: {path}" + ("" if args.dry_run else f"  (undo: --undo {path})"),
                  file=sys.stderr)

    if args.dry_run:
        print(f"Dry run: {len(todo)} would be marked watched.", file=sys.stderr)
    else:
        marked = sum(1 for r in report if r["outcome"] == "watched")
        print(f"Done: {marked} marked watched, {failed} failed (of {len(to_mark)}).",
              file=sys.stderr)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
