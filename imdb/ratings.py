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
`accept` before rating.

By default it skips rows with no match, no rating, or flagged for review; pass
--include-flagged to rate review rows too. Filters: --min-rating, --only-positive,
--limit.

Your current IMDb ratings are read first: a title already rated the same is left
alone, and one you rated differently keeps your rating unless --overwrite. Every
real run writes a report (default: out/imdb/reports/ratings-<time>.json) with
what each title had before; --undo <report> puts exactly that back — deletes a
rating that wasn't there (and the watched mark the rating added), restores one
that was — and skips titles you have re-rated since.

Examples:
    # for real, only movies you rated 7+
    python -m imdb.ratings --from-matches matches.json --min-rating 7

    # undo that run
    python -m imdb.ratings --undo out/imdb/reports/ratings-20261011-093000.json
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
    delete_rating,
    fetch_user_state,
    field_map_from_args,
    load_matches,
    load_records,
    make_session,
    rate_title,
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
        help="records file to resolve then rate (.json or .csv)",
    )
    src.add_argument(
        "--from-matches",
        help="a matches file from `imdb.search` (skip resolving) — the usual path",
    )
    src.add_argument(
        "--undo", metavar="REPORT",
        help="put back what a previous run's report says each title had before",
    )

    p.add_argument(
        "--dry-run", action="store_true",
        help="show what would change without touching IMDb",
    )
    p.add_argument(
        "--overwrite", action="store_true",
        help="replace a rating you already have on IMDb (default: keep yours)",
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

    p.add_argument(
        "--report",
        help="per-item report path (default for a real run: out/imdb/reports/ratings-<time>.json)",
    )
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
    titles only (tt...), and the row must carry a 1..10 rating.
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


def _label(m: Match) -> str:
    """Human line for a match, showing the rating and any title mismatch."""
    base = f"{m.imdb_const} {m.imdb_title or m.src_title or ''}".strip()
    r = _rating_value(m)
    if r is not None:
        base += f"  ({r}/10)"
    if m.src_title and m.imdb_title and m.src_title != m.imdb_title:
        base += f"  (from: {m.src_title})"
    if m.ambiguous and m.review:
        base += f"  [review: {m.review}]"
    return base


def _is_auth_error(err: Exception) -> bool:
    return "Authentication required" in str(err) or "FORBIDDEN" in str(err)


def plan_rating(m: Match, current: dict | None, args) -> tuple[str, str]:
    """(action, note) for one title given what IMDb holds for you now:
    action is "rate", "unchanged", "kept" or "unknown"."""
    want = _rating_value(m)
    if current is None:
        return "unknown", "IMDb returned no state for this title"
    if current["rating"] == want:
        return "unchanged", f"already {want}/10"
    if current["rating"] is not None and not args.overwrite:
        return "kept", f"yours is {current['rating']}/10 (--overwrite to replace)"
    return "rate", ""


def plan_undo(row: dict, current: dict | None) -> tuple[str, str]:
    """(action, note) for one report row: "clear", "restore", "unchanged",
    "kept" or "unknown"."""
    if current is None:
        return "unknown", "IMDb returned no state for this title"
    now = current["rating"]
    if now == row["rating"]:
        if row["previous_rating"] is None:
            return "clear", ""
        return "restore", f"back to {row['previous_rating']}/10"
    if now == row["previous_rating"]:
        # the write never landed, or the rating was put back since (a mark left
        # behind may be yours by now, so it stays)
        return "unchanged", "already as before the run"
    return "kept", f"rating changed since ({now}/10)" if now else "rating removed since"


def _undoable(row: dict) -> bool:
    """Rows a run may have changed: done, cut short mid-request, or failed (a
    failed write can still have landed). "planned" was never sent."""
    outcome = row.get("outcome") or ""
    return outcome in ("rated", "attempted") or outcome.startswith("error")


def _rate(session, const: str, rating: int) -> None:
    res = rate_title(session, const, rating)
    if (res.get("rating") or {}).get("value") != rating:
        raise RuntimeError("IMDb did not confirm the rating")


def _open_report(rows: list[dict], args, kind: str) -> Path | None:
    """Write the report before anything changes, so a bad path stops the run
    early. None for a dry run without --report."""
    if not args.report and args.dry_run:
        return None
    return write_report(rows, args.report, kind)


def run_undo(args, session) -> int:
    if args.report and Path(args.report).resolve() == Path(args.undo).resolve():
        print("ERROR: --report would overwrite the report being undone", file=sys.stderr)
        return 2
    rows = json.loads(Path(args.undo).read_text(encoding="utf-8"))
    if any("previous_rating" not in r or "was_watched" not in r for r in rows):
        print(f"ERROR: {args.undo} has no previous ratings to put back "
              "(written by an older version?); nothing changed", file=sys.stderr)
        return 2
    rows = [r for r in rows if _undoable(r)]
    if args.limit is not None:
        rows = rows[: args.limit]
    state = fetch_user_state(session, [r["const"] for r in rows])
    print(f"{len(rows)} rating(s) in the report to undo"
          + ("  [DRY RUN]" if args.dry_run else ""), file=sys.stderr)

    plan = [(row, *plan_undo(row, state.get(row["const"]))) for row in rows]
    report = [{"const": row["const"], "outcome": action if action in ("kept", "unknown", "unchanged")
               else "dry-run" if args.dry_run else "planned"} for row, action, _ in plan]
    path = _open_report(report, args, "ratings-undo")
    failed = 0
    try:
        for i, ((row, action, note), out) in enumerate(zip(plan, report), 1):
            const = row["const"]
            tag = f"[{i}/{len(rows)}] {const}"
            if out["outcome"] != "planned":
                failed += action == "unknown"
                verb = f"would {action}" if out["outcome"] == "dry-run" else f"= {action}:"
                print(f"{tag} {verb} {note}".rstrip(), file=sys.stderr)
                continue
            out["outcome"] = "attempted"
            try:
                if action == "restore":
                    _rate(session, const, row["previous_rating"])
                    outcome = "restored"
                else:
                    if not delete_rating(session, const):
                        raise RuntimeError("IMDb did not confirm the rating removal")
                    # the rating marked it watched; take that back too if it wasn't before
                    if not row["was_watched"]:
                        try:
                            res = unmark_watched(session, const)
                            if not res.get("success"):
                                raise RuntimeError("not successful")
                        except Exception as err:  # a retry would see it as "unchanged"
                            raise RuntimeError("rating cleared, but the watched mark stayed"
                                               f" ({err}); remove it on IMDb") from err
                    outcome = "cleared"
                print(f"{tag} + {outcome} {note}".rstrip(), file=sys.stderr)
            except Exception as err:  # noqa: BLE001 — one item must not kill the run
                failed += 1
                outcome = f"error: {err}"
                print(f"{tag} x FAILED: {err}", file=sys.stderr)
            out["outcome"] = outcome
            if _is_auth_error(outcome):
                print("Aborting: not authenticated (bad/expired cookies).", file=sys.stderr)
                break
            if i < len(rows):
                time.sleep(args.pause)
    finally:
        if path:
            write_report(report, str(path), "ratings-undo")
            print(f"Report: {path}", file=sys.stderr)
    return 0 if failed == 0 else 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.limit is not None and args.limit < 0:
        print("ERROR: --limit must be 0 or more", file=sys.stderr)
        return 2

    # Every path reads your current ratings first, so even a dry run needs cookies.
    cookie = resolve_cookie(
        args.cookie, args.cookie_file,
        use_browser=not args.no_browser_cookie, browsers=args.browsers,
    )
    session = make_session(cookie=cookie, need_auth=True)

    try:
        if args.undo:
            return run_undo(args, session)
        return run_rate(args, session)
    except OSError as err:  # mostly: the report can't be written
        print(f"ERROR: {err}", file=sys.stderr)
        return 2


def run_rate(args, session) -> int:
    matches = load_or_resolve(args, session)
    if not matches:
        return 1

    # Split into to-rate vs skipped.
    to_rate: list[Match] = []
    seen: set[str] = set()
    skipped: dict[str, int] = {}
    for m in matches:
        ok, reason = eligible(m, args)
        if ok and m.imdb_const in seen:  # first rating in the file wins
            ok, reason = False, "repeated-in-file"
        if ok:
            to_rate.append(m)
            seen.add(m.imdb_const)
        else:
            skipped[reason] = skipped.get(reason, 0) + 1
    if args.limit is not None:  # --limit 0 must rate nothing, not everything
        to_rate = to_rate[: args.limit]

    skip_note = ", ".join(f"{k}={v}" for k, v in sorted(skipped.items())) or "none"
    if skipped.get("review"):
        skip_note += " (edit their 'decision' to accept, or pass --include-flagged)"
    print(
        f"{len(to_rate)} to rate, skipped: {skip_note}"
        + ("  [DRY RUN]" if args.dry_run else ""),
        file=sys.stderr,
    )

    state = fetch_user_state(session, [m.imdb_const for m in to_rate])
    report: list[dict] = []
    todo: list[tuple[dict, str]] = []
    for m in to_rate:
        label = _label(m)
        current = state.get(m.imdb_const)
        action, note = plan_rating(m, current, args)
        # what --undo needs to put this title back
        row = {"const": m.imdb_const, "rating": _rating_value(m),
               "previous_rating": (current or {}).get("rating"),
               "was_watched": (current or {}).get("watched")}
        if action != "rate":
            print(f"  = {action}: {label}  {note}", file=sys.stderr)
            row["outcome"] = action
        else:
            row["outcome"] = "dry-run" if args.dry_run else "planned"
            todo.append((row, ("? " if m.ambiguous else "") + label))
        report.append(row)

    path = _open_report(report, args, "ratings")
    try:
        for i, (row, label) in enumerate(todo, 1):
            if args.dry_run:
                print(f"[{i}/{len(todo)}] would rate {label}", file=sys.stderr)
                continue
            row["outcome"] = "attempted"  # on disk before the request: undo must see it
            if path:
                write_report(report, str(path), "ratings")
            try:
                _rate(session, row["const"], row["rating"])
                row["outcome"] = "rated"
                print(f"[{i}/{len(todo)}] + rated: {label}", file=sys.stderr)
            except Exception as err:  # noqa: BLE001 — one item must not kill the run
                row["outcome"] = f"error: {err}"
                print(f"[{i}/{len(todo)}] x FAILED {label}: {err}", file=sys.stderr)
                if _is_auth_error(err):
                    print("Aborting: not authenticated (bad/expired cookies).", file=sys.stderr)
                    break
            if i < len(todo):
                time.sleep(args.pause)
    finally:
        if path:  # again, with the outcomes; also on Ctrl-C or a crash mid-run
            write_report(report, str(path), "ratings")
            print(f"Report: {path}" + ("" if args.dry_run else f"  (undo: --undo {path})"),
                  file=sys.stderr)

    counts: dict[str, int] = {}
    for row in report:
        o = row["outcome"]
        key = ("failed" if o.startswith("error") or o == "unknown"
               else "not-run" if o == "planned" else o)
        counts[key] = counts.get(key, 0) + 1
    summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "nothing"
    print(("Dry run: " if args.dry_run else "Done: ") + summary, file=sys.stderr)
    return 0 if not counts.get("failed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
