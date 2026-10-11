"""imdb.lists: adding to a list, and the removal modes."""

from __future__ import annotations

import json

from imdb import lists, search

from .conftest import AUTH


def matches_file(tmp_path, records: list[dict]):
    """A matches file the way a user gets one: records with known ids -> imdb.search."""
    src = tmp_path / "records.json"
    src.write_text(json.dumps(records), encoding="utf-8")
    out = tmp_path / "matches.json"
    assert search.main([str(src), "--const-field", "const", "-o", str(out)]) == 0
    return out


def test_adds_titles_and_skips_those_already_in_the_list(imdb, tmp_path):
    ls = imdb.new_list("ls1")
    imdb.put(ls, "tt1")
    m = matches_file(tmp_path, [
        {"const": "tt1", "title": "One"},
        {"const": "tt2", "title": "Two"},
    ])

    assert lists.main(["--from-matches", str(m), "--list-id", ls, *AUTH]) == 0
    assert lists.main(["--from-matches", str(m), "--list-id", ls, *AUTH]) == 0

    assert imdb.consts(ls) == ["tt1", "tt2"]


def test_mark_watched_reaches_titles_already_in_the_list(imdb, tmp_path):
    ls = imdb.new_list("ls1")
    imdb.put(ls, "tt1")
    m = matches_file(tmp_path, [{"const": "tt1", "title": "One"}])

    assert lists.main(["--from-matches", str(m), "--list-id", ls, "--mark-watched", *AUTH]) == 0

    assert imdb.consts(ls) == ["tt1"]
    assert imdb.watched == {"tt1"}


def test_dedupe_keeps_the_oldest_copy_even_within_one_minute(imdb):
    ls = imdb.new_list("ls1")
    # IMDb drops zero seconds: "06:01Z" is older than "06:01:18Z"
    newer = imdb.put(ls, "tt1", added="2026-10-11T06:01:18Z")
    older = imdb.put(ls, "tt1", added="2026-10-11T06:01Z")
    other = imdb.put(ls, "tt2", added="2026-10-11T07:00:00Z")

    assert lists.main(["--list-id", ls, "--dedupe", *AUTH]) == 0

    assert imdb.item_ids(ls) == [older, other]
    assert newer not in imdb.item_ids(ls)


def test_dedupe_leaves_entries_that_are_not_titles_or_people(imdb):
    ls = imdb.new_list("ls1", list_type="IMAGES")
    images = [imdb.put(ls, None, typename="Image") for _ in range(3)]

    assert lists.main(["--list-id", ls, "--dedupe", *AUTH]) == 0

    assert imdb.item_ids(ls) == images


def test_remove_drops_every_copy_of_the_titles_in_the_file_and_nothing_else(imdb, tmp_path):
    ls = imdb.new_list("ls1")
    imdb.put(ls, "tt1")
    keep = imdb.put(ls, "tt2")
    imdb.put(ls, "tt1")
    m = matches_file(tmp_path, [{"const": "tt1", "title": "One"}])

    assert lists.main(["--from-matches", str(m), "--list-id", ls, "--remove", *AUTH]) == 0

    assert imdb.item_ids(ls) == [keep]


def test_dry_run_removes_nothing(imdb):
    ls = imdb.new_list("ls1")
    imdb.put(ls, "tt1")
    imdb.put(ls, "tt1")

    assert lists.main(["--list-id", ls, "--empty", "--dry-run", *AUTH]) == 0

    assert len(imdb.item_ids(ls)) == 2


def test_limit_zero_removes_nothing(imdb):
    ls = imdb.new_list("ls1")
    imdb.put(ls, "tt1")

    assert lists.main(["--list-id", ls, "--empty", "--limit", "0", *AUTH]) == 0

    assert imdb.consts(ls) == ["tt1"]


def test_empty_across_all_lists_is_refused(imdb):
    for lid in ("ls1", "ls2"):
        imdb.new_list(lid)
        imdb.put(lid, "tt1")

    assert lists.main(["--all-lists", "--empty", *AUTH]) == 2

    assert imdb.consts("ls1") == ["tt1"] and imdb.consts("ls2") == ["tt1"]


def test_all_lists_dedupes_every_list(imdb):
    for lid in ("ls1", "ls2"):
        imdb.new_list(lid)
        imdb.put(lid, "tt1", added="2026-01-01T00:00:00Z")
        imdb.put(lid, "tt1", added="2026-02-01T00:00:00Z")

    assert lists.main(["--all-lists", "--dedupe", *AUTH]) == 0

    assert imdb.consts("ls1") == ["tt1"] and imdb.consts("ls2") == ["tt1"]


def test_unconfirmed_removals_fail_the_run_and_the_report_says_which(imdb, tmp_path):
    ls = imdb.new_list("ls1")
    gone = imdb.put(ls, "tt1")
    stuck = imdb.put(ls, "tt2")
    imdb.refuse_removal.add(stuck)
    report = tmp_path / "removed.json"

    assert lists.main(["--list-id", ls, "--empty", "--report", str(report), *AUTH]) == 1

    outcomes = {r["item_id"]: r["outcome"] for r in json.loads(report.read_text())}
    assert outcomes == {gone: "removed", stuck: "failed"}
    assert imdb.item_ids(ls) == [stuck]


def test_limit_zero_adds_nothing(imdb, tmp_path):
    ls = imdb.new_list("ls1")
    m = matches_file(tmp_path, [{"const": "tt1", "title": "One"}])

    assert lists.main(["--from-matches", str(m), "--list-id", ls, "--limit", "0", *AUTH]) == 0

    assert imdb.consts(ls) == []


def decide(path, decisions: dict[str, str]):
    """Edit `decision` in a matches file by const, the way a user reviews it."""
    rows = json.loads(path.read_text(encoding="utf-8"))
    for row in rows:
        row["decision"] = decisions.get(row["imdb_const"], row["decision"])
    path.write_text(json.dumps(rows), encoding="utf-8")


def test_only_accepted_rows_are_added_unless_flagged_ones_are_asked_for(imdb, tmp_path):
    ls = imdb.new_list("ls1")
    m = matches_file(tmp_path, [{"const": c, "title": c} for c in ("tt1", "tt2", "tt3")])
    decide(m, {"tt2": "reject", "tt3": "review"})

    assert lists.main(["--from-matches", str(m), "--list-id", ls, *AUTH]) == 0
    assert imdb.consts(ls) == ["tt1"]

    assert lists.main(["--from-matches", str(m), "--list-id", ls, "--include-flagged", *AUTH]) == 0
    assert imdb.consts(ls) == ["tt1", "tt3"]


def test_remove_leaves_titles_whose_row_was_rejected(imdb, tmp_path):
    ls = imdb.new_list("ls1")
    imdb.put(ls, "tt1")
    imdb.put(ls, "tt2")
    m = matches_file(tmp_path, [{"const": "tt1", "title": "One"}, {"const": "tt2", "title": "Two"}])
    decide(m, {"tt2": "reject"})

    assert lists.main(["--from-matches", str(m), "--list-id", ls, "--remove", *AUTH]) == 0

    assert imdb.consts(ls) == ["tt2"]


def test_dry_run_adds_and_marks_nothing(imdb, tmp_path):
    ls = imdb.new_list("ls1")
    imdb.put(ls, "tt1")
    m = matches_file(tmp_path, [{"const": "tt1", "title": "One"}, {"const": "tt2", "title": "Two"}])

    assert lists.main(["--from-matches", str(m), "--list-id", ls,
                       "--mark-watched", "--dry-run", *AUTH]) == 0

    assert imdb.consts(ls) == ["tt1"]
    assert imdb.watched == set()


def test_dedupe_and_add_see_entries_past_the_first_page(imdb, tmp_path):
    ls = imdb.new_list("ls1")
    for const in ("tt1", "tt2", "tt3", "tt4"):
        imdb.put(ls, const, added="2026-01-01T00:00:00Z")
    late_copy = imdb.put(ls, "tt4", added="2026-02-01T00:00:00Z")
    imdb.page_size = 2

    assert lists.main(["--list-id", ls, "--dedupe", *AUTH]) == 0
    assert imdb.consts(ls) == ["tt1", "tt2", "tt3", "tt4"]
    assert late_copy not in imdb.item_ids(ls)

    m = matches_file(tmp_path, [{"const": "tt4", "title": "Four"}])
    assert lists.main(["--from-matches", str(m), "--list-id", ls, *AUTH]) == 0
    assert imdb.consts(ls) == ["tt1", "tt2", "tt3", "tt4"]


def test_empty_clears_a_list_bigger_than_one_removal_batch(imdb):
    ls = imdb.new_list("ls1")
    for i in range(51):
        imdb.put(ls, f"tt{i}")

    assert lists.main(["--list-id", ls, "--empty", *AUTH]) == 0

    assert imdb.consts(ls) == []


def test_prune_makes_the_list_match_the_file(imdb, tmp_path):
    ls = imdb.new_list("ls1")
    imdb.put(ls, "tt1")
    imdb.put(ls, "tt9")              # not in the file: goes
    person = imdb.put(ls, "nm1", typename="Name")  # a titles file says nothing of people
    image = imdb.put(ls, None, typename="Image")
    m = matches_file(tmp_path, [{"const": c, "title": c} for c in ("tt1", "tt2", "tt3")])
    decide(m, {"tt3": "review"})     # might belong: kept if present, not added

    imdb.put(ls, "tt3")
    assert lists.main(["--from-matches", str(m), "--list-id", ls, "--prune", *AUTH]) == 0

    assert imdb.consts(ls) == ["tt1", "nm1", None, "tt3", "tt2"]
    assert {person, image} <= set(imdb.item_ids(ls))


def test_prune_is_refused_while_some_rows_have_no_imdb_id(imdb, tmp_path):
    ls = imdb.new_list("ls1")
    imdb.put(ls, "tt9")
    m = matches_file(tmp_path, [{"const": "tt1", "title": "One"}])
    rows = json.loads(m.read_text(encoding="utf-8"))
    rows.append({**rows[0], "imdb_const": None, "decision": "unmatched", "src_title": "Lost"})
    m.write_text(json.dumps(rows), encoding="utf-8")

    assert lists.main(["--from-matches", str(m), "--list-id", ls, "--prune", *AUTH]) == 2

    assert imdb.consts(ls) == ["tt9"]


def test_prune_dry_run_changes_nothing(imdb, tmp_path, capsys):
    ls = imdb.new_list("ls1")
    imdb.put(ls, "tt9")
    m = matches_file(tmp_path, [{"const": "tt1", "title": "One"}])

    assert lists.main(["--from-matches", str(m), "--list-id", ls, "--prune", "--dry-run", *AUTH]) == 0

    assert imdb.consts(ls) == ["tt9"]
    assert "tt9" in capsys.readouterr().err  # shows what would go


def test_prune_drops_rejected_and_filtered_out_titles_and_reports_them(imdb, tmp_path):
    ls = imdb.new_list("ls1")
    rejected = imdb.put(ls, "tt1")
    low = imdb.put(ls, "tt2")
    imdb.put(ls, "tt3")
    src = tmp_path / "kp.json"
    src.write_text(json.dumps([{"const": "tt1", "title": "One", "user_rating": 9},
                               {"const": "tt2", "title": "Two", "user_rating": 4},
                               {"const": "tt3", "title": "Three", "user_rating": 8}]),
                   encoding="utf-8")
    m = tmp_path / "matches.json"
    assert search.main([str(src), "--const-field", "const", "-o", str(m)]) == 0
    decide(m, {"tt1": "reject"})
    report = tmp_path / "sync.json"

    assert lists.main(["--from-matches", str(m), "--list-id", ls, "--prune", "--min-rating", "7",
                       "--report", str(report), *AUTH]) == 0

    assert imdb.consts(ls) == ["tt3"]
    added = json.loads(report.read_text(encoding="utf-8"))
    assert added == []  # nothing to add; the removals went to their own file
    pruned = json.loads((tmp_path / "sync.prune.json").read_text(encoding="utf-8"))
    assert {(r["item_id"], r["outcome"]) for r in pruned} == {(rejected, "removed"), (low, "removed")}


def test_prune_does_not_mix_with_removal_modes(imdb, tmp_path):
    ls = imdb.new_list("ls1")
    imdb.put(ls, "tt9")
    m = matches_file(tmp_path, [{"const": "tt1", "title": "One"}])

    assert lists.main(["--from-matches", str(m), "--list-id", ls, "--prune", "--remove", *AUTH]) == 2

    assert imdb.consts(ls) == ["tt9"]


def test_prune_keeps_titles_under_review_whatever_the_filters(imdb, tmp_path):
    ls = imdb.new_list("ls1")
    imdb.put(ls, "tt1")
    src = tmp_path / "kp.json"
    src.write_text(json.dumps([{"const": "tt1", "title": "One", "user_rating": 4},
                               {"const": "tt2", "title": "Two", "user_rating": 8}]),
                   encoding="utf-8")
    m = tmp_path / "matches.json"
    assert search.main([str(src), "--const-field", "const", "-o", str(m)]) == 0
    decide(m, {"tt1": "review"})

    assert lists.main(["--from-matches", str(m), "--list-id", ls, "--prune",
                       "--include-flagged", "--min-rating", "7", *AUTH]) == 0

    assert imdb.consts(ls) == ["tt1", "tt2"]


def test_prune_with_every_row_rejected_empties_the_titles(imdb, tmp_path):
    ls = imdb.new_list("ls1")
    imdb.put(ls, "tt1")
    imdb.put(ls, "tt9")
    imdb.put(ls, "nm1", typename="Name")
    m = matches_file(tmp_path, [{"const": "tt1", "title": "One"}])
    decide(m, {"tt1": "reject"})

    assert lists.main(["--from-matches", str(m), "--list-id", ls, "--prune", *AUTH]) == 0

    assert imdb.consts(ls) == ["nm1"]


def test_an_add_imdb_does_not_confirm_is_a_failure(imdb, tmp_path):
    ls = imdb.new_list("ls1")
    imdb.faults[("AddConstToList", "tt1")] = "null"
    m = matches_file(tmp_path, [{"const": "tt1", "title": "One"}])
    report = tmp_path / "r.json"

    assert lists.main(["--from-matches", str(m), "--list-id", ls, "--report", str(report), *AUTH]) == 1

    [row] = json.loads(report.read_text(encoding="utf-8"))
    assert row["outcome"].startswith("error")
