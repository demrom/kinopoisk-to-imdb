"""imdb.watched: mark watched without touching your own marks, undo only ours."""

from __future__ import annotations

import json

import pytest

from imdb import search, watched

from .conftest import AUTH


def matches(tmp_path, consts: list[str]):
    src = tmp_path / "kp.json"
    src.write_text(json.dumps([{"const": c, "title": c} for c in consts]), encoding="utf-8")
    out = tmp_path / "matches.json"
    assert search.main([str(src), "--const-field", "const", "-o", str(out)]) == 0
    return out


def test_undo_removes_only_the_marks_this_run_added(imdb, tmp_path):
    imdb.watched.add("tt1")  # yours, from before
    m = matches(tmp_path, ["tt1", "tt2", "tt3"])
    report = tmp_path / "report.json"

    assert watched.main(["--from-matches", str(m), "--report", str(report), *AUTH]) == 0
    assert imdb.watched == {"tt1", "tt2", "tt3"}

    imdb.ratings["tt3"] = 8  # rated afterwards: that rating needs the mark
    assert watched.main(["--undo", str(report), *AUTH]) == 0

    assert imdb.watched == {"tt1", "tt3"}


def test_a_real_run_always_leaves_a_report_to_undo_with(imdb, tmp_path):
    m = matches(tmp_path, ["tt1"])

    assert watched.main(["--from-matches", str(m), *AUTH]) == 0
    assert imdb.watched == {"tt1"}

    [report] = (tmp_path / "out/imdb/reports").glob("watched-*.json")
    assert watched.main(["--undo", str(report), *AUTH]) == 0
    assert imdb.watched == set()


def test_dry_run_and_limit_zero_mark_nothing(imdb, tmp_path):
    m = matches(tmp_path, ["tt1"])

    assert watched.main(["--from-matches", str(m), "--dry-run", *AUTH]) == 0
    assert watched.main(["--from-matches", str(m), "--limit", "0", *AUTH]) == 0

    assert imdb.watched == set()


def test_a_report_from_an_older_version_is_refused(imdb, tmp_path):
    imdb.watched.add("tt1")  # old reports said "watched" even for marks that were yours
    old = tmp_path / "old.json"
    old.write_text(json.dumps([{"const": "tt1", "outcome": "watched"}]), encoding="utf-8")

    assert watched.main(["--undo", str(old), *AUTH]) == 2

    assert imdb.watched == {"tt1"}


def test_undo_also_reverts_a_mark_whose_answer_was_lost(imdb, tmp_path):
    imdb.faults[("AddWatchedTitle", "tt1")] = "lost-ack"
    m = matches(tmp_path, ["tt1"])
    report = tmp_path / "r.json"
    assert watched.main(["--from-matches", str(m), "--report", str(report), *AUTH]) == 1
    assert imdb.watched == {"tt1"}  # it landed after all

    assert watched.main(["--undo", str(report), *AUTH]) == 0

    assert imdb.watched == set()


def test_an_unwritable_report_stops_the_run_before_anything_changes(imdb, tmp_path):
    m = matches(tmp_path, ["tt1"])

    assert watched.main(["--from-matches", str(m), "--report", str(tmp_path), *AUTH]) != 0

    assert imdb.watched == set()


def test_a_title_twice_in_the_file_is_marked_once(imdb, tmp_path):
    m = matches(tmp_path, ["tt1", "tt1"])
    report = tmp_path / "r.json"

    assert watched.main(["--from-matches", str(m), "--report", str(report), *AUTH]) == 0

    assert [r["const"] for r in json.loads(report.read_text(encoding="utf-8"))] == ["tt1"]


def test_ctrl_c_leaves_titles_the_run_never_reached_alone(imdb, tmp_path):
    imdb.faults[("AddWatchedTitle", "tt2")] = "interrupt"
    m = matches(tmp_path, ["tt1", "tt2", "tt3"])
    report = tmp_path / "r.json"
    with pytest.raises(KeyboardInterrupt):
        watched.main(["--from-matches", str(m), "--report", str(report), *AUTH])
    assert imdb.watched == {"tt1", "tt2"}
    imdb.faults.clear()
    imdb.watched.add("tt3")  # never reached by the run; you mark it yourself later

    assert watched.main(["--undo", str(report), *AUTH]) == 0

    assert imdb.watched == {"tt3"}


def test_undo_refuses_to_write_its_report_over_the_one_it_reads(imdb, tmp_path):
    m = matches(tmp_path, ["tt1"])
    report = tmp_path / "r.json"
    assert watched.main(["--from-matches", str(m), "--report", str(report), *AUTH]) == 0
    before = report.read_text(encoding="utf-8")

    assert watched.main(["--undo", str(report), "--report", str(report), "--dry-run", *AUTH]) == 2

    assert report.read_text(encoding="utf-8") == before


def test_the_report_on_disk_already_names_the_title_being_marked(imdb, tmp_path):
    report = tmp_path / "r.json"
    seen = []
    imdb.faults[("AddWatchedTitle", "tt1")] = lambda: seen.append(
        json.loads(report.read_text(encoding="utf-8")))
    m = matches(tmp_path, ["tt1"])

    assert watched.main(["--from-matches", str(m), "--report", str(report), *AUTH]) == 0

    assert seen[0][0]["outcome"] == "attempted"
