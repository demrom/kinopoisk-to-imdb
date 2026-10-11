"""imdb.export: a list comes back out with your own ratings and watched marks."""

from __future__ import annotations

import json

import pytest

from imdb import export

from .conftest import AUTH


def run_export(tmp_path, list_id: str, *extra: str) -> tuple[int, list[dict]]:
    out = tmp_path / "list.json"
    code = export.main([list_id, "-o", str(out), *extra, *AUTH])
    return code, json.loads(out.read_text(encoding="utf-8")) if out.exists() else []


def test_rows_carry_your_rating_and_watched_mark(imdb, tmp_path):
    ls = imdb.new_list("ls1", name="Favorites")
    imdb.title("tt1", "Rated", 2001)
    imdb.title("tt2", "Watched only", 2002, "tvSeries")
    imdb.title("tt3", "Untouched", 2003)
    for const in ("tt1", "tt2", "tt3"):
        imdb.put(ls, const)
    imdb.ratings["tt1"] = 8
    imdb.watched |= {"tt1", "tt2"}

    code, rows = run_export(tmp_path, ls)

    assert code == 0
    assert [(r["const"], r["title"], r["kind"], r["user_rating"], r["watched"]) for r in rows] == [
        ("tt1", "Rated", "movie", 8, True),
        ("tt2", "Watched only", "tvSeries", None, True),
        ("tt3", "Untouched", "movie", None, False),
    ]
    assert {r["list_name"] for r in rows} == {"Favorites"}


def test_watched_only_and_rated_only_filter_rows(imdb, tmp_path):
    ls = imdb.new_list("ls1")
    for const in ("tt1", "tt2", "tt3"):
        imdb.put(ls, const)
    imdb.ratings["tt1"] = 7
    imdb.watched |= {"tt1", "tt2"}

    assert [r["const"] for r in run_export(tmp_path, ls, "--watched-only")[1]] == ["tt1", "tt2"]
    assert [r["const"] for r in run_export(tmp_path, ls, "--rated-only")[1]] == ["tt1"]


def test_people_and_images_are_kept_and_labelled(imdb, tmp_path):
    ls = imdb.new_list("ls1")
    imdb.put(ls, "nm1", typename="Name")
    imdb.put(ls, None, typename="Image")

    code, rows = run_export(tmp_path, ls)

    assert code == 0
    assert [(r["const"], r["kind"], r["url"]) for r in rows] == [
        ("nm1", "person", "https://www.imdb.com/name/nm1/"),
        (None, "image", None),
    ]


def test_a_list_longer_than_one_page_comes_back_whole_and_in_order(imdb, tmp_path):
    ls = imdb.new_list("ls1")
    consts = [f"tt{i}" for i in range(1, 8)]
    for const in consts:
        imdb.put(ls, const)
    imdb.page_size = 3

    code, rows = run_export(tmp_path, ls)

    assert code == 0
    assert [r["const"] for r in rows] == consts
    assert [r["list_position"] for r in rows] == list(range(1, 8))


@pytest.mark.parametrize("fault", ["no-pageinfo", "stale-cursor"])
def test_broken_paging_fails_instead_of_writing_a_truncated_list(imdb, tmp_path, fault):
    ls = imdb.new_list("ls1")
    for i in range(5):
        imdb.put(ls, f"tt{i}")
    imdb.page_size = 2
    imdb.paging_fault = fault
    previous = tmp_path / "list.json"
    previous.write_text("previous export", encoding="utf-8")

    code = export.main([ls, "-o", str(previous), *AUTH])

    assert code == 1
    assert previous.read_text(encoding="utf-8") == "previous export"
    assert imdb.pages_served <= 3  # stopped at the fault, did not keep paging


def test_an_unknown_list_fails_but_the_others_still_export(imdb, tmp_path):
    ls = imdb.new_list("ls1", name="Real")
    imdb.put(ls, "tt1")

    code = export.main(["lsMissing", ls, "--out-dir", str(tmp_path), *AUTH])

    assert code == 1
    [written] = tmp_path.glob("ls1-*.json")
    assert [r["const"] for r in json.loads(written.read_text(encoding="utf-8"))] == ["tt1"]
