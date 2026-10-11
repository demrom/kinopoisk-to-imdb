"""A fake IMDb behind the two HTTP endpoints the tool talks to.

Tests drive the real CLI entry points; only the network (and time.sleep) is
replaced. The fake keeps its own state — list entries, your ratings, your
watched marks — so a test asserts on what IMDb would hold afterwards, not on
which internal function was called.
"""

from __future__ import annotations

import itertools
import json
import re
import time
from dataclasses import dataclass, field
from urllib.parse import unquote

import pytest
import responses

from imdb.core import GRAPHQL_URL

SUGGEST_RE = re.compile(r"https://v3\.sg\.media-imdb\.com/suggestion/x/(?P<q>[^?]+)\.json.*")

# the field each GraphQL operation's body must select, checked before state changes
ROOT_FIELDS = {
    "ExportList": "list",
    "MyLists": "lists",
    "AddConstToList": "addItemToList",
    "RemoveItemsFromList": "removeItemsByItemIds",
    "UpdateTitleRating": "rateTitle",
    "DeleteTitleRating": "deleteTitleRating",
    "AddWatchedTitle": "addWatchedTitle",
    "RemoveWatchedTitle": "removeWatchedTitle",
}


@dataclass
class Title:
    const: str
    title: str
    year: int | None = None
    kind: str = "movie"  # IMDb titleType id: movie, tvSeries, videoGame, ...


@dataclass
class FakeImdb:
    titles: dict[str, Title] = field(default_factory=dict)
    # query -> suggestion entries, in the order IMDb would rank them
    suggestions: dict[str, list[str]] = field(default_factory=dict)
    # list id -> entries {itemId, const, createdDate, typename}
    lists: dict[str, dict] = field(default_factory=dict)
    ratings: dict[str, int] = field(default_factory=dict)
    watched: set[str] = field(default_factory=set)
    page_size: int = 250
    # break ExportList paging: "no-pageinfo", or "stale-cursor" (same cursor again)
    paging_fault: str | None = None
    pages_served: int = 0  # ExportList calls; capped so a paging loop fails, not hangs
    # item ids the remove mutation pretends not to have removed
    refuse_removal: set[str] = field(default_factory=set)
    _ids: itertools.count = field(default_factory=lambda: itertools.count(1000))

    # -- setup helpers ---------------------------------------------------- #
    def title(self, const: str, title: str, year: int | None = None, kind: str = "movie") -> str:
        self.titles[const] = Title(const, title, year, kind)
        return const

    def new_list(self, list_id: str, name: str = "Test", list_type: str = "TITLES") -> str:
        self.lists[list_id] = {"name": name, "type": list_type, "entries": []}
        return list_id

    def put(self, list_id: str, const: str | None, added: str = "2026-01-01T00:00:00Z",
            typename: str = "Title") -> str:
        item_id = f"li{next(self._ids)}"
        self.lists[list_id]["entries"].append(
            {"itemId": item_id, "const": const, "createdDate": added, "typename": typename}
        )
        return item_id

    def consts(self, list_id: str) -> list[str | None]:
        return [e["const"] for e in self.lists[list_id]["entries"]]

    def item_ids(self, list_id: str) -> list[str]:
        return [e["itemId"] for e in self.lists[list_id]["entries"]]

    # -- suggestion API ---------------------------------------------------- #
    def _suggest(self, request):
        query = unquote(SUGGEST_RE.match(request.url)["q"])
        entries = []
        for const in self.suggestions.get(query, []):
            t = self.titles[const]
            entries.append({"id": t.const, "l": t.title, "y": t.year, "qid": t.kind})
        return 200, {}, json.dumps({"d": entries})

    # -- GraphQL ----------------------------------------------------------- #
    def _graphql(self, request):
        body = json.loads(request.body)
        op, v = body.get("operationName"), body.get("variables") or {}
        handler = getattr(self, f"_op_{op}", None)
        if handler is None:
            return self._error(f"fake: no {op}")
        # what IMDb rejects: a query that doesn't define the named operation, and
        # (every operation here being personal) a request without your cookies
        if not re.search(rf"\b(query|mutation)\s+{op}\b", body.get("query") or ""):
            return self._error(f"Unknown operation named \"{op}\"")
        if not request.headers.get("Cookie"):
            return self._error("Authentication required")
        if not re.search(rf"\b{ROOT_FIELDS[op]}\b", body["query"]):
            return self._error(f"fake: {op} does not select {ROOT_FIELDS[op]}")
        return 200, {}, json.dumps({"data": handler(v)})

    @staticmethod
    def _error(message: str):
        return 200, {}, json.dumps({"errors": [{"message": message}]})

    def _title_node(self, const: str | None, typename: str) -> dict:
        if typename == "Name":
            return {"__typename": "Name", "id": const, "nameText": {"text": const}}
        if typename != "Title":
            return {"__typename": typename}
        t = self.titles.get(const) or Title(const, const)
        rating = self.ratings.get(const)
        return {
            "__typename": "Title",
            "id": const,
            "titleText": {"text": t.title},
            "originalTitleText": {"text": t.title},
            "titleType": {"id": t.kind},
            "releaseYear": {"year": t.year, "endYear": None},
            "userRating": {"value": rating, "date": "2026-01-01T00:00:00Z"} if rating else None,
            "userWatchedStatus": {"isWatched": const in self.watched, "firstWatched": None},
        }

    def _op_ExportList(self, v):
        lst = self.lists.get(v["id"])
        if lst is None:
            return {"list": None}
        self.pages_served += 1
        if self.pages_served > 100:
            raise RuntimeError("fake: endless paging")
        entries = lst["entries"]
        start = int(v.get("after") or 0)
        page = entries[start:start + min(v["first"], self.page_size)]
        end = start + len(page)
        page_info = {"hasNextPage": end < len(entries), "endCursor": str(end)}
        if self.paging_fault == "no-pageinfo":
            page_info = None
        elif self.paging_fault == "stale-cursor":
            page_info["endCursor"] = v.get("after") or "0"
        return {"list": {
            "id": v["id"],
            "name": {"originalText": lst["name"]},
            "items": {
                "total": len(entries),
                "pageInfo": page_info,
                "edges": [{"node": {
                    "itemId": e["itemId"],
                    "createdDate": e["createdDate"],
                    "item": self._title_node(e["const"], e["typename"]),
                }} for e in page],
            },
        }}

    def _op_MyLists(self, v):
        return {"lists": {"edges": [{"node": {
            "id": lid,
            "name": {"originalText": lst["name"]},
            "listType": {"id": lst["type"]},
            "items": {"total": len(lst["entries"])},
        }} for lid, lst in self.lists.items()]}}

    def _op_AddConstToList(self, v):
        item_id = self.put(v["listId"], v["constId"], added="2026-06-01T00:00:00Z")
        return {"addItemToList": {"listId": v["listId"], "modifiedItem": {"itemId": item_id}}}

    def _op_RemoveItemsFromList(self, v):
        lst = self.lists[v["listId"]]
        gone = [i for i in v["itemIds"] if i not in self.refuse_removal]
        lst["entries"] = [e for e in lst["entries"] if e["itemId"] not in gone]
        return {"removeItemsByItemIds": {"listId": v["listId"], "itemIds": gone}}

    def _op_UpdateTitleRating(self, v):
        self.ratings[v["titleId"]] = v["rating"]
        self.watched.add(v["titleId"])  # rating a title marks it watched on IMDb
        return {"rateTitle": {"rating": {"value": v["rating"]}}}

    def _op_DeleteTitleRating(self, v):
        self.ratings.pop(v["titleId"], None)
        return {"deleteTitleRating": {"date": "2026-01-01"}}

    def _op_AddWatchedTitle(self, v):
        self.watched.add(v["titleId"])
        return {"addWatchedTitle": {"success": True, "message": None}}

    def _op_RemoveWatchedTitle(self, v):
        self.watched.discard(v["titleId"])
        return {"removeWatchedTitle": {"success": True, "message": None}}


@pytest.fixture
def imdb(monkeypatch):
    """The fake IMDb, wired in for the duration of one test."""
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    monkeypatch.delenv("IMDB_COOKIE", raising=False)
    monkeypatch.delenv("IMDB_LIST_ID", raising=False)
    fake = FakeImdb()
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.add_callback(responses.GET, SUGGEST_RE, callback=fake._suggest)
        rsps.add_callback(responses.POST, GRAPHQL_URL, callback=fake._graphql)
        yield fake


# Cookie flags every authenticated command gets in tests: a fixed cookie, and
# never a trip to the real browser.
AUTH = ["--cookie", "session-id=1", "--no-browser-cookie"]
