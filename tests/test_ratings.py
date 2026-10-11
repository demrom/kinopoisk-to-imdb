"""imdb.ratings: copy ratings without clobbering yours, and undo exactly."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from imdb import ratings, search

from .conftest import AUTH


def rated_matches(tmp_path, wanted: dict[str, int]):
    """A matches file carrying Kinopoisk ratings for already-known ids."""
    src = tmp_path / "kp.json"
    src.write_text(json.dumps([
        {"const": const, "title": const, "user_rating": r} for const, r in wanted.items()
    ]), encoding="utf-8")
    out = tmp_path / "matches.json"
    assert search.main([str(src), "--const-field", "const", "-o", str(out)]) == 0
    return out


def test_a_different_rating_you_already_have_is_kept_unless_overwrite(imdb, tmp_path):
    imdb.ratings["tt1"] = 9
    m = rated_matches(tmp_path, {"tt1": 7, "tt2": 6})

    assert ratings.main(["--from-matches", str(m), *AUTH]) == 0
    assert imdb.ratings == {"tt1": 9, "tt2": 6}

    assert ratings.main(["--from-matches", str(m), "--overwrite", *AUTH]) == 0
    assert imdb.ratings == {"tt1": 7, "tt2": 6}


def run_and_report(tmp_path, m, *extra) -> str:
    report = str(tmp_path / f"report{len(list(tmp_path.glob('report*')))}.json")
    assert ratings.main(["--from-matches", str(m), "--report", report, *extra, *AUTH]) == 0
    return report


def test_undo_puts_back_exactly_what_was_there(imdb, tmp_path):
    imdb.ratings["tt1"] = 9          # yours, replaced with --overwrite
    imdb.watched |= {"tt1", "tt3"}   # tt3 watched but unrated
    imdb.ratings["tt4"] = 5          # already what the file says
    imdb.watched.add("tt4")
    m = rated_matches(tmp_path, {"tt1": 7, "tt2": 6, "tt3": 8, "tt4": 5})
    report = run_and_report(tmp_path, m, "--overwrite")
    assert imdb.ratings == {"tt1": 7, "tt2": 6, "tt3": 8, "tt4": 5}

    assert ratings.main(["--undo", report, *AUTH]) == 0

    assert imdb.ratings == {"tt1": 9, "tt4": 5}
    assert imdb.watched == {"tt1", "tt3", "tt4"}  # tt2's mark came from the rating


def test_undo_leaves_a_title_you_rerated_since(imdb, tmp_path):
    m = rated_matches(tmp_path, {"tt1": 7, "tt2": 6})
    report = run_and_report(tmp_path, m)
    imdb.ratings["tt1"] = 10  # changed your mind on IMDb afterwards

    assert ratings.main(["--undo", report, *AUTH]) == 0

    assert imdb.ratings == {"tt1": 10}


def test_a_real_run_always_leaves_a_report_to_undo_with(imdb, tmp_path):
    m = rated_matches(tmp_path, {"tt1": 7})

    assert ratings.main(["--from-matches", str(m), *AUTH]) == 0
    assert imdb.ratings == {"tt1": 7}

    [report] = (tmp_path / "out/imdb/reports").glob("ratings-*.json")
    assert ratings.main(["--undo", str(report), *AUTH]) == 0
    assert imdb.ratings == {}


def test_dry_run_and_limit_zero_rate_nothing(imdb, tmp_path):
    m = rated_matches(tmp_path, {"tt1": 7})

    assert ratings.main(["--from-matches", str(m), "--dry-run", *AUTH]) == 0
    assert ratings.main(["--from-matches", str(m), "--limit", "0", *AUTH]) == 0

    assert imdb.ratings == {}


def test_the_same_rating_you_already_have_is_not_rewritten(imdb, tmp_path):
    imdb.ratings["tt1"] = 7
    m = rated_matches(tmp_path, {"tt1": 7})

    assert ratings.main(["--from-matches", str(m), "--overwrite", *AUTH]) == 0

    assert imdb.ratings == {"tt1": 7}
    assert "tt1" not in imdb.rated_at  # still dated when you rated it, not today


def test_two_quick_runs_keep_both_reports(imdb, tmp_path):
    m = rated_matches(tmp_path, {"tt1": 7})

    assert ratings.main(["--from-matches", str(m), *AUTH]) == 0
    assert ratings.main(["--from-matches", str(m), *AUTH]) == 0

    assert len(list((tmp_path / "out/imdb/reports").glob("ratings-*.json"))) == 2


def test_an_unwritable_report_stops_the_run_before_anything_changes(imdb, tmp_path):
    m = rated_matches(tmp_path, {"tt1": 7})

    assert ratings.main(["--from-matches", str(m), "--report", str(tmp_path), *AUTH]) != 0

    assert imdb.ratings == {}


def test_a_title_twice_in_the_file_is_rated_once(imdb, tmp_path):
    src = tmp_path / "kp.json"
    src.write_text(json.dumps([{"const": "tt1", "title": "One", "user_rating": 7},
                               {"const": "tt1", "title": "One", "user_rating": 8}]),
                   encoding="utf-8")
    m = tmp_path / "matches.json"
    assert search.main([str(src), "--const-field", "const", "-o", str(m)]) == 0

    assert ratings.main(["--from-matches", str(m), *AUTH]) == 0

    assert imdb.ratings == {"tt1": 7}


def test_an_empty_answer_from_imdb_is_a_failure(imdb, tmp_path):
    imdb.faults[("UpdateTitleRating", "tt1")] = "null"
    m = rated_matches(tmp_path, {"tt1": 7})
    report = tmp_path / "r.json"

    assert ratings.main(["--from-matches", str(m), "--report", str(report), *AUTH]) == 1

    [row] = json.loads(report.read_text(encoding="utf-8"))
    assert row["outcome"].startswith("error")


def test_undo_also_reverts_a_write_whose_answer_was_lost(imdb, tmp_path):
    imdb.faults[("UpdateTitleRating", "tt1")] = "lost-ack"
    m = rated_matches(tmp_path, {"tt1": 7})
    report = tmp_path / "r.json"
    assert ratings.main(["--from-matches", str(m), "--report", str(report), *AUTH]) == 1
    assert imdb.ratings == {"tt1": 7}  # it landed after all

    assert ratings.main(["--undo", str(report), *AUTH]) == 0

    assert imdb.ratings == {} and imdb.watched == set()


def test_undo_leaves_the_mark_of_a_rating_you_deleted_yourself(imdb, tmp_path):
    m = rated_matches(tmp_path, {"tt1": 7})
    report = run_and_report(tmp_path, m)
    del imdb.ratings["tt1"]  # you delete it on IMDb; the watched mark stays

    assert ratings.main(["--undo", report, *AUTH]) == 0

    assert (imdb.ratings, imdb.watched) == ({}, {"tt1"})


def test_a_report_from_an_older_version_is_refused(imdb, tmp_path):
    imdb.ratings["tt1"] = 8
    old = tmp_path / "old.json"
    old.write_text(json.dumps([{"const": "tt1", "outcome": "rated 8", "rating": 8}]), encoding="utf-8")

    assert ratings.main(["--undo", str(old), *AUTH]) == 2

    assert imdb.ratings == {"tt1": 8}


def test_ctrl_c_mid_run_still_leaves_a_report_that_undoes_it(imdb, tmp_path):
    imdb.faults[("UpdateTitleRating", "tt2")] = "interrupt"
    m = rated_matches(tmp_path, {"tt1": 7, "tt2": 6, "tt3": 5})
    report = tmp_path / "r.json"

    with pytest.raises(KeyboardInterrupt):
        ratings.main(["--from-matches", str(m), "--report", str(report), *AUTH])
    assert imdb.ratings == {"tt1": 7, "tt2": 6}

    imdb.faults.clear()
    imdb.ratings["tt3"] = 5  # never reached by the run; you rate it yourself later
    assert ratings.main(["--undo", str(report), *AUTH]) == 0
    assert imdb.ratings == {"tt3": 5}


def test_undo_keeps_the_mark_while_the_rating_it_belongs_to_stays(imdb, tmp_path):
    m = rated_matches(tmp_path, {"tt1": 7})
    report = run_and_report(tmp_path, m)
    imdb.faults[("DeleteTitleRating", "tt1")] = "null"

    assert ratings.main(["--undo", report, *AUTH]) == 1

    assert (imdb.ratings, imdb.watched) == ({"tt1": 7}, {"tt1"})


def test_undo_refuses_to_write_its_report_over_the_one_it_reads(imdb, tmp_path):
    m = rated_matches(tmp_path, {"tt1": 7})
    report = run_and_report(tmp_path, m)
    before = Path(report).read_text(encoding="utf-8")

    assert ratings.main(["--undo", report, "--report", report, *AUTH]) == 2

    assert Path(report).read_text(encoding="utf-8") == before and imdb.ratings == {"tt1": 7}


def test_the_report_on_disk_already_names_the_title_being_rated(imdb, tmp_path):
    report = tmp_path / "r.json"
    seen = []
    imdb.faults[("UpdateTitleRating", "tt1")] = lambda: seen.append(
        json.loads(report.read_text(encoding="utf-8")))
    m = rated_matches(tmp_path, {"tt1": 7})

    assert ratings.main(["--from-matches", str(m), "--report", str(report), *AUTH]) == 0

    assert seen[0][0]["outcome"] == "attempted"


def test_undo_never_clobbers_its_source_through_a_temp_file(imdb, tmp_path):
    m = rated_matches(tmp_path, {"tt1": 7})
    src = tmp_path / "r.json.tmp"
    src.write_text(Path(run_and_report(tmp_path, m)).read_text(encoding="utf-8"), encoding="utf-8")
    before = src.read_text(encoding="utf-8")

    ratings.main(["--undo", str(src), "--report", str(tmp_path / "r.json"), "--limit", "0", *AUTH])

    assert src.read_text(encoding="utf-8") == before


def test_undo_that_cannot_remove_the_mark_says_to_do_it_by_hand(imdb, tmp_path):
    m = rated_matches(tmp_path, {"tt1": 7})
    report = run_and_report(tmp_path, m)
    imdb.faults[("RemoveWatchedTitle", "tt1")] = "error"
    out = tmp_path / "undo.json"

    assert ratings.main(["--undo", report, "--report", str(out), *AUTH]) == 1

    [row] = json.loads(out.read_text(encoding="utf-8"))
    assert "remove it on IMDb" in row["outcome"] and "transient failure" in row["outcome"]
