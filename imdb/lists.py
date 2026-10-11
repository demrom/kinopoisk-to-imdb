"""Add resolved IMDb consts to one of your IMDb lists — or clean one up.

The normal flow is two steps: `python -m imdb.search` resolves your records into
a matches file you can eyeball, then this reads that file and adds each title to
a target list via the AddConstToList GraphQL mutation. This mutates your
account, so it needs your IMDb cookies (pulled from the browser via
fetch_cookies by default). You can also skip the file and resolve a records file
on the fly by passing it positionally.

IMDb stores a second copy of a title without complaining, so the list is read
before adding and titles already in it are skipped. The same reading powers the
removal modes: --dedupe drops repeated entries (keeping the earliest copy),
--empty drops every entry, and --remove drops the titles your input file names.
All take --dry-run, and removal is not undoable.

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

    # clean up: drop repeated entries, or clear the list entirely
    python -m imdb.lists --list-id ls123456789 --dedupe --dry-run
    python -m imdb.lists --list-id ls123456789 --empty --dry-run

    # drop exactly what a file lists (the add path, in reverse)
    python -m imdb.lists watched.json --const-field const --list-id ls123456789 --remove
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from .core import (
    DECISION_NO,
    DECISION_REVIEW,
    LIST_ENV,
    Match,
    add_cookie_args,
    add_field_map_args,
    add_to_list,
    fetch_list_entries,
    fetch_user_lists,
    field_map_from_args,
    load_matches,
    load_records,
    make_session,
    mark_watched,
    remove_list_items,
    resolve_cookie,
    resolve_records,
)

# How many entries one removal mutation takes. IMDb accepts a batch; keep it
# modest so a failure costs one small chunk, not the whole run.
REMOVE_BATCH = 50

# Sorts after any real added-date, so an undated entry never wins a dedupe.
_NO_DATE = datetime.max.replace(tzinfo=UTC)


def _is_duplicate(err: str) -> bool:
    low = err.lower()
    return any(w in low for w in ("already", "duplicate", "exists"))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    src = p.add_mutually_exclusive_group()
    src.add_argument(
        "records", nargs="?",
        help="records file to resolve then import (.json or .csv)",
    )
    src.add_argument(
        "--from-matches",
        help="a matches file from `imdb.search` (skip resolving) — the usual path",
    )
    src.add_argument(
        "--dedupe", action="store_true",
        help="add nothing; remove entries that repeat a title already in the "
             "list, keeping the earliest copy",
    )
    src.add_argument(
        "--empty", action="store_true",
        help="add nothing; remove every entry from the list",
    )

    p.add_argument(
        "--list-id",
        help=f"target IMDb list id, e.g. ls123456789 (or ${LIST_ENV})",
    )
    p.add_argument(
        "--all-lists", action="store_true",
        help="apply a removal mode to every list you own (--dedupe / --remove); "
             "refused with --empty",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="show what would be added (or removed) without touching IMDb",
    )
    p.add_argument(
        "--remove", action="store_true",
        help="remove the input file's titles from the list instead of adding "
             "them (every copy of each); the same row filters apply",
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


def _entry_label(node: dict, position: int) -> str:
    """Human line for one list entry: what it is and when it landed."""
    item = node.get("item") or {}
    title = (
        (item.get("titleText") or {}).get("text")
        or (item.get("nameText") or {}).get("text")
        or ""
    )
    year = (item.get("releaseYear") or {}).get("year")
    added = (node.get("createdDate") or "")[:10]
    return f"{item.get('id')} {title} ({year})  #{position} added {added}"


def plan_removals(
        nodes: list[dict],
        *,
        empty: bool = False,
        consts: set[str] | None = None,
) -> list[tuple[str, str]]:
    """(item_id, label) of the entries to remove, in one of three modes.

    --empty takes everything; a `consts` set takes every entry holding one of
    those titles (all copies of it); otherwise it is a dedupe, which keeps the
    *oldest* entry of each const — the one added first, by the date it carries,
    with its position in the list as the tie-break — and drops the rest.
    """
    if empty:
        return [
            (node.get("itemId"), _entry_label(node, position))
            for position, node in enumerate(nodes, 1)
        ]
    if consts is not None:
        return [
            (node.get("itemId"), _entry_label(node, position))
            for position, node in enumerate(nodes, 1)
            if (node.get("item") or {}).get("id") in consts
        ]

    # Dedupe: pick the survivor per const first, then drop everything else. A
    # missing date sorts last so it never beats an entry that has one. An entry
    # with no const (an image, say: the query reads ids of titles and people
    # only) is never anyone's duplicate.
    # Dates are parsed, not compared as text: IMDb drops zero seconds
    # ("06:01Z" vs "06:01:18Z"), and as strings the shorter form sorts later.
    keep: dict[str, tuple[tuple[datetime, int], str]] = {}
    for position, node in enumerate(nodes, 1):
        const = (node.get("item") or {}).get("id")
        if not const:
            continue
        try:
            added = datetime.fromisoformat(node.get("createdDate") or "")
        except ValueError:
            added = _NO_DATE
        if added.tzinfo is None:  # a naive date can't be compared with aware ones
            added = added.replace(tzinfo=UTC)
        key = (added, position)
        if const not in keep or key < keep[const][0]:
            keep[const] = (key, node.get("itemId"))
    survivors = {item_id for _, item_id in keep.values()}
    return [
        (node.get("itemId"), _entry_label(node, position))
        for position, node in enumerate(nodes, 1)
        if (node.get("item") or {}).get("id")
        and node.get("itemId") not in survivors
    ]


def _report_path(base: str | None, list_id: str, many: bool) -> str | None:
    """One report per list when several are processed: out/x.json -> out/x.ls123.json."""
    if not base or not many:
        return base
    p = Path(base)
    return str(p.with_suffix(f".{list_id}{p.suffix}"))


def run_removals(args, session, list_id: str | None, consts: set[str] | None = None) -> int:
    """Pick the target list(s) for a removal mode and run it over each."""
    if args.all_lists:
        if args.empty:
            print(
                "ERROR: --empty with --all-lists would clear every list you "
                "own — refused",
                file=sys.stderr,
            )
            return 2
        lists = fetch_user_lists(session)
        print(f"{len(lists)} lists on the account:", file=sys.stderr)
        for lst in lists:
            print(
                f"  {lst['id']}  {lst['total'] or 0:>5} items  {lst['type'] or '-':<7} {lst['name']}",
                file=sys.stderr,
            )
        # an unknown size is read like any other list; only a known 0 is skipped
        targets = [lst["id"] for lst in lists if lst["total"] != 0]
    elif list_id:
        targets = [list_id]
    else:
        print(
            f"ERROR: no list id (pass --list-id, --all-lists or set ${LIST_ENV})",
            file=sys.stderr,
        )
        return 2

    rc = 0
    for target in targets:
        rc |= remove_entries(
            args, session, target, consts=consts,
            report=_report_path(args.report, target, len(targets) > 1),
        )
    return rc


def remove_entries(
        args,
        session,
        list_id: str,
        consts: set[str] | None = None,
        report: str | None = None,
) -> int:
    """The removal path: read one list, then take out what the mode says."""
    print(f"Reading list {list_id}...", file=sys.stderr)
    meta, nodes = fetch_list_entries(
        session, list_id,
        progress=lambda got, total: print(f"  {got}/{total} entries", file=sys.stderr),
    )
    name = (meta.get("name") or {}).get("originalText") or list_id
    doomed = plan_removals(nodes, empty=args.empty, consts=consts)
    if args.limit is not None:  # --limit 0 must remove nothing, not everything
        doomed = doomed[: args.limit]

    if args.empty:
        what = "every entry"
    elif consts is not None:
        in_list = {(n.get("item") or {}).get("id") for n in nodes} & consts
        what = (
            f"the titles listed in the input file ({len(in_list)} of {len(consts)} "
            "are in this list)"
        )
    else:
        what = "duplicate entries (keeping the oldest copy of each)"
    print(
        f"{name!r}: {len(nodes)} entries, {len(doomed)} to remove — {what}"
        + ("  [DRY RUN]" if args.dry_run else ""),
        file=sys.stderr,
    )
    for i, (_, label) in enumerate(doomed, 1):
        print(f"[{i}/{len(doomed)}] - {label}", file=sys.stderr)
    report = report if report is not None else args.report
    # Each entry's fate: "planned" until IMDb confirms it gone ("removed") or not.
    outcome = {iid: "planned" for iid, _ in doomed}

    def write_report() -> None:
        if not (report and doomed):
            return
        report_path = Path(report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(
                [{"item_id": iid, "entry": label, "outcome": outcome[iid]}
                 for iid, label in doomed],
                ensure_ascii=False, indent=2,
            ),
            encoding="utf-8",
        )

    if args.dry_run or not doomed:
        write_report()
        print(
            f"Dry run: {len(doomed)} would be removed." if args.dry_run
            else "Nothing to remove.",
            file=sys.stderr,
        )
        return 0

    removed = failed = 0
    for start in range(0, len(doomed), REMOVE_BATCH):
        batch = doomed[start:start + REMOVE_BATCH]
        ids = [iid for iid, _ in batch]
        try:
            confirmed = set(remove_list_items(session, list_id, ids))
        except Exception as err:  # noqa: BLE001 — one batch must not kill the run
            confirmed = set()
            print(f"  x FAILED batch of {len(ids)}: {err}", file=sys.stderr)
        for iid in ids:
            outcome[iid] = "removed" if iid in confirmed else "failed"
        unconfirmed = sum(1 for iid in ids if iid not in confirmed)
        removed += len(ids) - unconfirmed
        failed += unconfirmed
        if confirmed and unconfirmed:
            print(
                f"WARNING: asked to remove {len(ids)}, IMDb confirmed "
                f"{len(ids) - unconfirmed}",
                file=sys.stderr,
            )
        print(f"  done {start + len(batch)}/{len(doomed)}", file=sys.stderr)
        if start + REMOVE_BATCH < len(doomed):
            time.sleep(args.pause)

    write_report()
    print(f"Done: {removed} removed, {failed} failed (of {len(doomed)}).", file=sys.stderr)
    return 0 if not failed else 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if not (args.records or args.from_matches or args.dedupe or args.empty):
        print(
            "ERROR: nothing to do — pass a records file, --from-matches, "
            "--dedupe or --empty",
            file=sys.stderr,
        )
        return 2
    if args.remove and (args.dedupe or args.empty):
        print("ERROR: --remove takes an input file, not --dedupe/--empty", file=sys.stderr)
        return 2
    if args.all_lists and not (args.dedupe or args.empty or args.remove):
        print("ERROR: --all-lists is for removal modes only, not adding", file=sys.stderr)
        return 2
    if args.limit is not None and args.limit < 0:
        print("ERROR: --limit must be 0 or more", file=sys.stderr)
        return 2

    list_id = args.list_id or os.environ.get(LIST_ENV)
    if not list_id and not args.dry_run and not args.all_lists:
        print(f"ERROR: no list id (pass --list-id or set ${LIST_ENV})", file=sys.stderr)
        return 2

    # Reading the list needs the cookies too (a private list is owner-only), so
    # they are resolved even for a dry run — that is what makes the preview
    # accurate about what is already there.
    cookie = resolve_cookie(
        args.cookie, args.cookie_file,
        use_browser=not args.no_browser_cookie, browsers=args.browsers,
    )
    session = make_session(cookie=cookie, need_auth=not args.dry_run)

    if args.dedupe or args.empty:
        return run_removals(args, session, list_id)

    matches = load_or_resolve(args, session)
    if not matches:
        return 1

    if args.remove:
        return run_removals(
            args, session, list_id,
            consts={m.imdb_const for m in matches if eligible(m, args)[0]},
        )

    # IMDb stores a second copy of a title without complaint, so "already in the
    # list" has to be read from the list itself before adding, not inferred from
    # a mutation error.
    existing: set[str] = set()
    if list_id and cookie:
        try:
            _, nodes = fetch_list_entries(session, list_id)
            existing = {(n.get("item") or {}).get("id") for n in nodes}
            print(f"List {list_id} holds {len(existing)} distinct titles.", file=sys.stderr)
        except Exception as err:  # noqa: BLE001 — fall back to adding blind
            print(
                f"WARNING: could not read list {list_id} ({err}); "
                "duplicates may be added",
                file=sys.stderr,
            )

    # Split into to-add vs skipped.
    to_add: list[Match] = []
    skipped: dict[str, int] = {}
    seen: set[str] = set()
    for m in matches:
        ok, reason = eligible(m, args)
        # --mark-watched still has work for a title already in the list: it
        # stays in the run, and the loop marks it without adding it again.
        if ok and m.imdb_const in existing and not args.mark_watched:
            ok, reason = False, "already-in-list"
        if ok and m.imdb_const in seen:
            ok, reason = False, "repeated-in-file"
        if ok:
            seen.add(m.imdb_const)
            to_add.append(m)
        else:
            skipped[reason] = skipped.get(reason, 0) + 1
    if args.limit is not None:  # --limit 0 must add nothing, not everything
        to_add = to_add[: args.limit]

    skip_note = ", ".join(f"{k}={v}" for k, v in sorted(skipped.items())) or "none"
    if skipped.get("review"):
        skip_note += " (edit their 'decision' to accept, or pass --include-flagged)"
    # titles already in the list ride along only for --mark-watched
    watch_only = sum(1 for m in to_add if m.imdb_const in existing)
    print(
        f"{len(to_add) - watch_only} to add"
        + (f", {watch_only} already in list to mark watched" if watch_only else "")
        + f", skipped: {skip_note}"
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
            verb = "already in list" if m.imdb_const in existing else "would add"
            print(f"[{i}/{total}] {mark} {verb} {label}{extra}", file=sys.stderr)
            report.append({"const": m.imdb_const, "outcome": "dry-run", "title": m.imdb_title})
            continue
        in_list = False
        try:
            if m.imdb_const in existing:
                dupes += 1
                in_list = True
                outcome = "duplicate"
                print(f"[{i}/{total}] = already in list: {label}", file=sys.stderr)
            else:
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
        also = f", {watch_only} already there" if watch_only else ""
        print(f"Dry run: {total - watch_only} would be added{also}{note}.", file=sys.stderr)
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
