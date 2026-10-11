"""imdb.search: Kinopoisk records -> IMDb consts, against faked suggestions."""

from __future__ import annotations

import json

from imdb import search


def resolve(tmp_path, record: dict) -> dict:
    """Run imdb.search on one Kinopoisk-shaped record, return its match row."""
    src = tmp_path / "kp.json"
    src.write_text(json.dumps([record]), encoding="utf-8")
    out = tmp_path / "matches.json"
    assert search.main([str(src), "-o", str(out)]) == 0
    [row] = json.loads(out.read_text(encoding="utf-8"))
    return row


def test_series_wins_over_a_same_name_film_when_the_source_is_a_series(imdb, tmp_path):
    # the film has the exact year, the series is a year off: type still decides
    imdb.title("tt100", "Succession", 2018, "movie")
    imdb.title("tt7660850", "Succession", 2019, "tvSeries")
    imdb.suggestions["Succession"] = ["tt100", "tt7660850"]

    row = resolve(tmp_path, {"original_title": "Succession", "title": "Наследники",
                             "year": 2018, "kind": "series"})

    assert (row["decision"], row["imdb_const"]) == ("accept", "tt7660850")


def test_film_wins_over_a_same_name_series_when_the_source_is_a_film(imdb, tmp_path):
    imdb.title("tt200", "Dune", 2021, "tvSeries")
    imdb.title("tt1160419", "Dune", 2021, "movie")
    imdb.suggestions["Dune"] = ["tt200", "tt1160419"]

    row = resolve(tmp_path, {"original_title": "Dune", "year": 2021, "kind": "film"})

    assert (row["decision"], row["imdb_const"]) == ("accept", "tt1160419")


def test_a_video_game_with_the_same_name_and_year_is_never_accepted(imdb, tmp_path):
    imdb.title("tt300", "The Batman", 2022, "videoGame")
    imdb.suggestions["The Batman"] = ["tt300"]

    row = resolve(tmp_path, {"original_title": "The Batman", "year": 2022, "kind": "film"})

    assert row["decision"] == "review"
    assert "unexpected type" in row["review"]


def test_a_video_game_with_the_exact_year_does_not_outrank_the_film(imdb, tmp_path):
    imdb.title("tt300", "The Batman", 2022, "videoGame")
    imdb.title("tt1877830", "The Batman", 2021, "movie")
    imdb.suggestions["The Batman"] = ["tt300", "tt1877830"]

    row = resolve(tmp_path, {"original_title": "The Batman", "year": 2022, "kind": "film"})

    assert (row["decision"], row["imdb_const"]) == ("accept", "tt1877830")


def test_falls_back_to_the_localized_title_when_the_original_finds_nothing(imdb, tmp_path):
    imdb.title("tt400", "Brat", 1997, "movie")
    imdb.suggestions["Брат"] = ["tt400"]

    row = resolve(tmp_path, {"original_title": "Brat 1997", "title": "Брат",
                             "year": 1997, "kind": "film"})

    assert row["imdb_const"] == "tt400"


def test_nothing_found_is_unmatched(imdb, tmp_path):
    row = resolve(tmp_path, {"original_title": "No Such Film", "year": 2001, "kind": "film"})

    assert (row["decision"], row["imdb_const"]) == ("unmatched", None)


def test_two_equally_good_candidates_go_to_review(imdb, tmp_path):
    imdb.title("tt501", "Heat", 1995, "movie")
    imdb.title("tt502", "Heat", 1995, "movie")
    imdb.suggestions["Heat"] = ["tt501", "tt502"]

    row = resolve(tmp_path, {"original_title": "Heat", "year": 1995, "kind": "film"})

    assert row["decision"] == "review"
    assert "close alternative" in row["review"]


def test_one_imdb_id_given_to_two_different_titles_sends_both_to_review(imdb, tmp_path):
    src = tmp_path / "kp.json"
    src.write_text(json.dumps([
        {"const": "tt1", "title": "One"},
        {"const": "tt1", "title": "Another one"},
        {"const": "tt2", "title": "Two"},
    ]), encoding="utf-8")
    out = tmp_path / "matches.json"

    assert search.main([str(src), "--const-field", "const", "-o", str(out)]) == 0

    decisions = {(r["src_title"], r["decision"]) for r in json.loads(out.read_text(encoding="utf-8"))}
    assert decisions == {("One", "review"), ("Another one", "review"), ("Two", "accept")}
