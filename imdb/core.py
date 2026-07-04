"""Shared core for the IMDb importer.

Holds everything both steps need:

  * HTTP session + cookie handling (cookies come from the browser via
    fetch_cookies, same as the Kinopoisk scrapers; --cookie / --cookie-file /
    IMDB_COOKIE override);
  * the two header sets IMDb wants — one for the public suggestion/search API
    (v3.sg.media-imdb.com) and one for the authenticated GraphQL API
    (api.graphql.imdb.com);
  * data models (ImdbTitle for a search hit, Match for a resolved Kinopoisk
    movie);
  * the suggestion-search client and the Kinopoisk->IMDb matching logic;
  * the GraphQL client and the AddConstToList mutation;
  * loading a Kinopoisk export and saving the resolved matches.

Only imdb.lists needs cookies (it mutates your list). imdb.search talks to the
public suggestion API and needs nothing.

Dependencies: requests (+ rookiepy for browser cookies).
"""

from __future__ import annotations

import csv
import difflib
import json
import os
import re
import sys
import time
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import quote

import requests

# Public suggestion/search API (autocomplete). Returns titles, names and videos.
SUGGEST_URL = "https://v3.sg.media-imdb.com/suggestion/x/{query}.json"
# Authenticated GraphQL API (list mutations live here).
GRAPHQL_URL = "https://api.graphql.imdb.com/"

# Env var names for secrets / defaults (so they don't leak into argv).
COOKIE_ENV = "IMDB_COOKIE"  # the full Cookie string from the browser
UA_ENV = "IMDB_UA"  # optional: custom User-Agent
LIST_ENV = "IMDB_LIST_ID"  # optional: default target list id (ls...)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/149.0.0.0 Safari/537.36"
)

# Headers for the public suggestion API — cross-site, no cookies. Mirrors a
# working DevTools request (Copy as cURL).
SUGGEST_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
    "Origin": "https://www.imdb.com",
    "Referer": "https://www.imdb.com/",
    "Sec-Ch-Ua": '"Chromium";v="149", "Not)A;Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"macOS"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "cross-site",
    "X-Imdb-Pace-Migration": "true",
    "X-Imdb-Weblab-Discover-Modern": "T2",
}

# Headers for the GraphQL API — same-site, cookies added at request time. The
# x-imdb-* headers identify the web client; without them IMDb may reject.
GRAPHQL_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/graphql+json, application/json",
    "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
    "Content-Type": "application/json",
    "Origin": "https://www.imdb.com",
    "Referer": "https://www.imdb.com/",
    "Sec-Ch-Ua": '"Chromium";v="149", "Not)A;Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"macOS"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
    "X-Imdb-Client-Name": "imdb-web-next-localized",
    "X-Imdb-User-Country": "US",
    "X-Imdb-User-Language": "en-US",
}

# IMDb title-type ids (the "qid" field in suggestion results), split into
# series-like vs movie-like, so we can prefer the same kind Kinopoisk had.
SERIES_QIDS = {"tvSeries", "tvMiniSeries", "tvSpecial", "tvShort", "podcastSeries"}
MOVIE_QIDS = {"movie", "tvMovie", "short", "video", "musicVideo", "videoGame"}

YEAR_RE = re.compile(r"(\d{4})")


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
@dataclass
class ImdbTitle:
    """One hit from the suggestion API — a title (tt...) or, when searching
    people, a name (nm...).

    Same shape either way: `title` holds the label (the title, or the person's
    name), `stars` the top-billed cast or the person's known-for credit. For
    names the API sends no year/kind/category, so those stay None.
    """

    const: str  # tt...
    title: str | None = None
    year: int | None = None
    kind: str | None = None  # qid: movie / tvSeries / tvMiniSeries / ...
    category: str | None = None  # q: "feature" / "TV series" / ...
    stars: str | None = None  # s: top-billed cast, for eyeballing
    rank: int | None = None  # popularity rank (lower = more popular)
    image_url: str | None = None

    @classmethod
    def from_suggestion(cls, entry: dict) -> "ImdbTitle":
        img = entry.get("i") or {}
        return cls(
            const=entry.get("id"),
            title=entry.get("l"),
            year=entry.get("y"),
            kind=entry.get("qid"),
            category=entry.get("q"),
            stars=entry.get("s"),
            rank=entry.get("rank"),
            image_url=img.get("imageUrl"),
        )


# The `decision` field is the human-editable verdict that drives the import.
DECISION_ACCEPT = "accept"  # import it
DECISION_REVIEW = "review"  # needs a human look before importing
DECISION_REJECT = "reject"  # do not import
DECISION_UNMATCHED = "unmatched"  # nothing found — fix imdb_const by hand or drop
# Values that mean "do not import" when read back from an edited file.
DECISION_NO = {DECISION_REJECT, DECISION_UNMATCHED, "skip", "no", "false", "-"}


@dataclass
class Match:
    """One source record resolved (or not) to an IMDb const.

    The `src_*` fields are lifted from the input record via a FieldMap, so this
    works for any JSON shape, not only a Kinopoisk export.

    `decision` is the actionable verdict: `imdb.search` seeds it (accept/review/
    unmatched) and you edit it in the file (e.g. review -> accept or reject);
    `imdb.lists` imports only rows whose decision is accept. `review` explains,
    in words, why a row was flagged for a look.
    """

    # --- verdict (edit this in the file to decide what gets imported) ---
    decision: str | None = None  # accept | review | reject | unmatched
    review: str | None = None  # why it's flagged, in words

    # --- source record (pulled out via FieldMap) ---
    src_id: str | int | None = None
    src_title: str | None = None  # the primary title used for searching
    src_year: str | None = None
    src_kind: str | None = None
    src_rating: int | None = None
    src_sentiment: str | None = None

    # --- resolved IMDb title ---
    imdb_const: str | None = None
    imdb_title: str | None = None
    imdb_year: int | None = None
    imdb_kind: str | None = None

    # --- scoring / provenance ---
    query_used: str | None = None
    score: float | None = None  # overall confidence (title + year + kind)
    title_score: float | None = None  # pure title similarity, source vs match
    ambiguous: bool = False  # something looks off — eyeball before importing
    alternatives: list[dict] = field(default_factory=list)
    error: str | None = None

    @property
    def matched(self) -> bool:
        return bool(self.imdb_const)


def default_decision(m: "Match") -> str:
    """The verdict `imdb.search` seeds before any human edits."""
    if not m.matched:
        return DECISION_UNMATCHED
    return DECISION_REVIEW if m.ambiguous else DECISION_ACCEPT


# Which kind values mean "series" vs "movie" (lowercased). Unknown values give
# no bias. Extend the series set with --series-values for exotic inputs.
SERIES_KIND_VALUES = {
    "series", "tv", "tvseries", "tv series", "tvminiseries", "tv mini-series",
    "mini-series", "show", "сериал",
}
MOVIE_KIND_VALUES = {"film", "movie", "feature", "tvmovie", "tv movie", "short", "фильм"}


@dataclass
class FieldMap:
    """How to read a source record: which JSON keys hold what.

    `search` is a priority list of keys to build the IMDb query from (and to
    score candidate titles against). The rest are optional; None disables that
    signal. Defaults match a Kinopoisk export.
    """

    search: list[str] = field(default_factory=lambda: ["original_title", "title"])
    year: str | None = "year"
    kind: str | None = "kind"
    const: str | None = None  # a key that already holds a tt/nm const -> skip search
    rating: str | None = "user_rating"
    sentiment: str | None = "user_rating_sentiment"
    id: str | None = "kp_id"
    series_values: set[str] = field(default_factory=lambda: set(SERIES_KIND_VALUES))
    entity: str = "title"  # "title" -> resolve to tt...; "person" -> resolve to nm...
    transliterate: bool = True  # person mode: fall back to a transliterated query

    @property
    def is_person(self) -> bool:
        return self.entity == "person"

    def titles(self, record: dict) -> list[str]:
        """Non-empty search values from `record`, in configured order, deduped."""
        out: list[str] = []
        for key in self.search:
            val = record.get(key)
            if val and str(val).strip() and str(val) not in out:
                out.append(str(val))
        return out

    def want_series(self, record: dict) -> bool | None:
        """True/False if the kind value is known to be series/movie, else None."""
        if not self.kind:
            return None
        val = record.get(self.kind)
        if val in (None, ""):
            return None
        v = str(val).strip().lower()
        if v in self.series_values:
            return True
        if v in MOVIE_KIND_VALUES:
            return False
        return None


# --------------------------------------------------------------------------- #
# HTTP session & cookies
# --------------------------------------------------------------------------- #
def resolve_cookie(
        cli_cookie: str | None = None,
        cli_cookie_file: str | None = None,
        *,
        use_browser: bool = True,
        browsers: list[str] | None = None,
        verbose: bool = False,
) -> str | None:
    """Where to get the IMDb Cookie from.

    Priority: --cookie > --cookie-file > env IMDB_COOKIE > browser (rookiepy via
    fetch_cookies). Returns None only if everything failed.
    """
    if cli_cookie:
        return cli_cookie
    if cli_cookie_file:
        return Path(cli_cookie_file).read_text(encoding="utf-8").strip()
    env_cookie = os.environ.get(COOKIE_ENV)
    if env_cookie:
        return env_cookie

    if not use_browser:
        return None

    # Last resort: pull live cookies from the browser. Imported lazily so callers
    # that pass cookies explicitly never need fetch_cookies/rookiepy installed.
    try:
        from fetch_cookies import get_cookie_string
    except ImportError as e:
        print(f"WARNING: cannot import fetch_cookies ({e}); no cookies", file=sys.stderr)
        return None
    try:
        cookie = get_cookie_string("imdb", browsers=browsers, verbose=verbose)
        print("Cookie: pulled from the browser via fetch_cookies", file=sys.stderr)
        return cookie
    except Exception as e:  # noqa: BLE001 — browser locked / not logged in / etc.
        print(f"WARNING: failed to pull cookies from the browser: {e}", file=sys.stderr)
        return None


def _cookie_value(cookie: str, name: str) -> str | None:
    """Pull one value out of a 'k=v; k=v' Cookie string."""
    for part in cookie.split(";"):
        k, _, v = part.strip().partition("=")
        if k == name:
            return v
    return None


def make_session(
        cookie: str | None = None,
        user_agent: str | None = None,
        *,
        need_auth: bool = False,
) -> requests.Session:
    """Build a session. Default headers suit the public suggestion API.

    The Cookie is NOT put on the session: the suggestion API lives on a
    different domain (media-imdb.com) the browser never sends imdb.com cookies
    to, so we stash the cookie separately and graphql() attaches it per-request.
    Pass need_auth=True to warn loudly when auth is required but missing.
    """
    session = requests.Session()
    session.headers.update(SUGGEST_HEADERS)
    ua = user_agent or os.environ.get(UA_ENV)
    if ua:
        session.headers["User-Agent"] = ua

    if cookie:
        # IMDb wants x-amzn-sessionid to match the session-id cookie for
        # mutations; derive it so we don't rely on the caller pasting it.
        sid = _cookie_value(cookie, "session-id")
        session.cookie_str = cookie  # type: ignore[attr-defined]
        session.amzn_session_id = sid  # type: ignore[attr-defined]
    elif need_auth:
        print(
            f"WARNING: no IMDb Cookie (no {COOKIE_ENV}, --cookie, --cookie-file, "
            "or browser cookies). List mutations require login and will fail.",
            file=sys.stderr,
        )
    return session


# --------------------------------------------------------------------------- #
# Suggestion / search
# --------------------------------------------------------------------------- #
def _norm(text: str | None) -> str:
    """Fold a title for comparison: strip accents, lowercase, drop punctuation."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def _parse_year(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    m = YEAR_RE.search(str(value))
    return int(m.group(1)) if m else None


# Practical Cyrillic -> Latin, for names IMDb doesn't index in Cyrillic. Not a
# strict standard (IMDb spellings vary: Sergei/Sergey, Alexey/Aleksei) — the
# fuzzy title scorer absorbs the slack. Used only as a fallback when a record
# has no Latin form of its own.
_CYRILLIC_TO_LATIN = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}

_LATIN_RE = re.compile(r"[A-Za-z]")


def _has_latin(text: str | None) -> bool:
    """True if `text` already carries a Latin form worth searching IMDb by."""
    return bool(text) and bool(_LATIN_RE.search(text))


def transliterate(text: str | None) -> str:
    """Best-effort Cyrillic -> Latin, preserving case and non-Cyrillic chars."""
    if not text:
        return ""
    out: list[str] = []
    for ch in text:
        rep = _CYRILLIC_TO_LATIN.get(ch.lower())
        if rep is None:
            out.append(ch)  # already Latin, digits, spaces, punctuation
        elif ch.isupper() and rep:
            out.append(rep[0].upper() + rep[1:])
        else:
            out.append(rep)
    return "".join(out)


def suggest(
        session: requests.Session,
        query: str,
        *,
        include_videos: bool = False,
        retries: int = 3,
        pause: float = 1.0,
        timeout: int = 20,
) -> list[dict]:
    """Raw suggestion entries for a query. The query goes in the URL path."""
    q = re.sub(r"\s+", " ", (query or "").strip())
    if not q:
        return []
    url = SUGGEST_URL.format(query=quote(q, safe=""))
    params = {"includeVideos": "1" if include_videos else "0"}

    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            resp = session.get(
                url, params=params, headers=SUGGEST_HEADERS, timeout=timeout
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("d", []) or []
        except Exception as err:  # noqa: BLE001 — retry on any failure
            last_err = err
            if attempt < retries:
                time.sleep(pause * attempt)
    raise RuntimeError(f"suggestion failed for {q!r}: {last_err}")


def _search_by_prefix(
        session: requests.Session, query: str, prefix: str, **kw
) -> list[ImdbTitle]:
    """suggest() filtered to entries whose const starts with `prefix`."""
    return [
        ImdbTitle.from_suggestion(e)
        for e in suggest(session, query, **kw)
        if str(e.get("id", "")).startswith(prefix)
    ]


def search_titles(session: requests.Session, query: str, **kw) -> list[ImdbTitle]:
    """suggest() filtered to titles (id starts with 'tt')."""
    return _search_by_prefix(session, query, "tt", **kw)


def search_people(session: requests.Session, query: str, **kw) -> list[ImdbTitle]:
    """suggest() filtered to names/people (id starts with 'nm')."""
    return _search_by_prefix(session, query, "nm", **kw)


# --------------------------------------------------------------------------- #
# Kinopoisk -> IMDb matching
# --------------------------------------------------------------------------- #
def _title_sim(candidate: str | None, wanted: list[str]) -> float:
    """Best fuzzy similarity of a candidate title against any wanted title."""
    cand = _norm(candidate)
    if not cand:
        return 0.0
    best = 0.0
    for w in wanted:
        nw = _norm(w)
        if not nw:
            continue
        best = max(best, difflib.SequenceMatcher(None, cand, nw).ratio())
    return best


def score_candidate(
        cand: ImdbTitle,
        *,
        wanted_titles: list[str],
        want_year: int | None,
        want_series: bool | None,
) -> float:
    """Confidence that `cand` is the right title, roughly 0..1."""
    score = _title_sim(cand.title, wanted_titles)

    # Year agreement is a strong signal (foreign titles collide a lot).
    if want_year and cand.year:
        diff = abs(cand.year - want_year)
        if diff == 0:
            score += 0.15
        elif diff == 1:
            score += 0.07
        elif diff <= 2:
            score += 0.02
        else:
            score -= 0.15

    # Kind agreement (movie vs series).
    if want_series is not None and cand.kind:
        is_series = cand.kind in SERIES_QIDS
        is_movie = cand.kind in MOVIE_QIDS
        if (want_series and is_series) or (not want_series and is_movie):
            score += 0.06
        elif is_series or is_movie:
            score -= 0.08

    # Not clamped to 1.0: the year/kind bonuses must still separate two titles
    # that both match by name (e.g. the "Succession" series vs the movie), so we
    # rank on the raw score and only clamp for display.
    return max(0.0, score)


def resolve_const(
        session: requests.Session,
        *,
        titles: list[str],
        year: str | int | None = None,
        want_series: bool | None = None,
        pause: float = 0.5,
        search_fn=search_titles,
        noun: str = "title",
        transliterate_fallback: bool = False,
) -> Match:
    """Resolve a list of candidate strings to the best IMDb const.

    `titles` is a priority list of search strings (e.g. original title first,
    then a localized title). Searches by each in turn, merges candidates, scores
    them by title/year/kind, picks the best and records close runners-up. Only
    fills the imdb_* / query_used / score fields; callers own the src_* fields.

    `search_fn` selects what to resolve to: `search_titles` (tt..., the default)
    or `search_people` (nm...); `noun` just labels errors/review notes to match.
    Resolving people passes no year/kind, so scoring falls back to name
    similarity alone.

    `transliterate_fallback`: when no candidate string has a Latin form, add a
    transliterated guess (IMDb doesn't index Cyrillic names). Such a match is
    flagged for review, since the transliteration is only a best guess.
    """
    m = Match()
    want_year = _parse_year(year)
    wanted_titles = [t for t in titles if t]
    if not wanted_titles:
        m.error = f"no {noun} to search by"
        return m

    # Nothing Latin to search by? IMDb won't match Cyrillic names, so add a
    # transliterated fallback — both as a query and as something to score the
    # Latin candidates against (comparing Latin vs Cyrillic would score ~0).
    used_transliteration = False
    if transliterate_fallback and not any(_has_latin(t) for t in wanted_titles):
        for t in list(wanted_titles):
            tr = transliterate(t)
            if tr and tr != t and tr not in wanted_titles:
                wanted_titles.append(tr)
                used_transliteration = True

    # Query order as given: original title first (IMDb is indexed by
    # original/English titles), fallbacks after. Dedup, preserve order.
    queries: list[str] = []
    for q in wanted_titles:
        if q not in queries:
            queries.append(q)

    scored: dict[str, tuple[float, ImdbTitle, str]] = {}  # const -> (score, cand, query)
    try:
        for i, q in enumerate(queries):
            if i:
                time.sleep(pause)
            for cand in search_fn(session, q):
                if not cand.const:
                    continue
                s = score_candidate(
                    cand,
                    wanted_titles=wanted_titles,
                    want_year=want_year,
                    want_series=want_series,
                )
                prev = scored.get(cand.const)
                if prev is None or s > prev[0]:
                    scored[cand.const] = (s, cand, q)
            # Good enough on the original title? Don't bother with the fallback.
            if scored and max(v[0] for v in scored.values()) >= 0.85:
                break
    except Exception as err:  # noqa: BLE001 — one movie must not kill the run
        m.error = str(err)
        return m

    if not scored:
        m.error = f"no IMDb {noun} found"
        return m

    ranked = sorted(scored.values(), key=lambda v: (-v[0], (v[1].rank or 10**9)))
    best_score, best, best_query = ranked[0]
    title_score = _title_sim(best.title, wanted_titles)
    m.imdb_const = best.const
    m.imdb_title = best.title
    m.imdb_year = best.year
    m.imdb_kind = best.kind
    m.query_used = best_query
    m.score = round(min(best_score, 1.0), 3)  # clamp for display; ranked on raw
    m.title_score = round(title_score, 3)
    m.alternatives = [
        {
            "const": c.const,
            "title": c.title,
            "year": c.year,
            "kind": c.kind,
            "score": round(min(s, 1.0), 3),
        }
        for s, c, _ in ranked[1:4]
    ]

    # Spell out anything that looks off, so a human can eyeball the right rows.
    reasons: list[str] = []
    if title_score < 0.5:
        reasons.append(f"{noun} differs from source")
    if want_year and best.year is not None:
        diff = abs(best.year - want_year)
        if diff > 1:
            reasons.append(f"year off by {diff} ({best.year} vs {want_year})")
    elif want_year and best.year is None:
        reasons.append("no year to compare")
    if len(ranked) > 1 and best_score - ranked[1][0] < 0.05:
        reasons.append("close alternative exists")
    if best_score < 0.6 and not reasons:
        reasons.append("low overall confidence")
    # Transliteration is a guess (and can land a plausible-but-wrong namesake),
    # so never auto-accept it — always route it through review.
    if used_transliteration:
        reasons.append(f"matched by transliteration ({best_query!r}) — verify")

    if reasons:
        m.ambiguous = True
        m.review = "; ".join(reasons)
    return m


# --------------------------------------------------------------------------- #
# GraphQL
# --------------------------------------------------------------------------- #
# Trimmed AddConstToList mutation: same operation the IMDb web app sends, but
# without the metadata fragments (we pass includeListItemMetadata: false, so
# the server never returns them). Validated against the live endpoint.
ADD_TO_LIST_MUTATION = """
mutation AddConstToList($listId: ID!, $constId: ID!) {
  addItemToList(input: {listId: $listId, item: {itemElementId: $constId}}) {
    listId
    modifiedItem {
      itemId
    }
  }
}
""".strip()

# Set / clear your personal rating (1..10) on a title — the same operations the
# IMDb web app sends. rateTitle is an upsert, so re-running is safe.
RATE_TITLE_MUTATION = """
mutation UpdateTitleRating($rating: Int!, $titleId: ID!) {
  rateTitle(input: {rating: $rating, titleId: $titleId}) {
    rating {
      value
    }
  }
}
""".strip()

DELETE_RATING_MUTATION = """
mutation DeleteTitleRating($titleId: ID!) {
  deleteTitleRating(input: {titleId: $titleId}) {
    date
  }
}
""".strip()


def graphql(
        session: requests.Session,
        query: str,
        variables: dict,
        operation_name: str | None = None,
        *,
        retries: int = 3,
        pause: float = 1.5,
        timeout: int = 30,
) -> dict:
    """POST a GraphQL request and return the parsed JSON (raises on errors)."""
    headers = dict(GRAPHQL_HEADERS)
    cookie = getattr(session, "cookie_str", None)
    if cookie:
        headers["Cookie"] = cookie
    sid = getattr(session, "amzn_session_id", None)
    if sid:
        headers["X-Amzn-Sessionid"] = sid

    payload: dict = {"query": query, "variables": variables}
    if operation_name:
        payload["operationName"] = operation_name

    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            resp = session.post(
                GRAPHQL_URL, json=payload, headers=headers, timeout=timeout
            )
            # 4xx bodies still carry useful GraphQL errors — parse before raising.
            try:
                data = resp.json()
            except ValueError:
                resp.raise_for_status()
                raise RuntimeError(f"non-JSON response (status {resp.status_code})")
            if data.get("errors"):
                msg = "; ".join(
                    e.get("message", str(e)) for e in data["errors"]
                )
                raise RuntimeError(msg)
            return data
        except Exception as err:  # noqa: BLE001 — retry on transient failures
            last_err = err
            # Auth failures won't fix themselves on retry — fail fast.
            if "Authentication required" in str(err) or "FORBIDDEN" in str(err):
                break
            if attempt < retries:
                time.sleep(pause * attempt)
    raise RuntimeError(str(last_err))


def add_to_list(session: requests.Session, list_id: str, const: str) -> dict:
    """Add one const (tt.../nm...) to an IMDb list. Returns the modified item."""
    data = graphql(
        session,
        ADD_TO_LIST_MUTATION,
        {"listId": list_id, "constId": const},
        operation_name="AddConstToList",
    )
    return (data.get("data") or {}).get("addItemToList") or {}


def rate_title(session: requests.Session, const: str, rating: int) -> dict:
    """Set your personal IMDb rating (1..10) on a title. Returns the new rating."""
    data = graphql(
        session,
        RATE_TITLE_MUTATION,
        {"titleId": const, "rating": int(rating)},
        operation_name="UpdateTitleRating",
    )
    return (data.get("data") or {}).get("rateTitle") or {}


def delete_rating(session: requests.Session, const: str) -> dict:
    """Clear your personal IMDb rating on a title (undo)."""
    data = graphql(
        session,
        DELETE_RATING_MUTATION,
        {"titleId": const},
        operation_name="DeleteTitleRating",
    )
    return (data.get("data") or {}).get("deleteTitleRating") or {}


# --------------------------------------------------------------------------- #
# Loading records and resolving them
# --------------------------------------------------------------------------- #
def load_records(path: Path) -> list[dict]:
    """Load an arbitrary list of records from a file.

    Accepts .json (a top-level list, or a {"movies"/"items": [...]} wrapper) or
    .csv. Returns plain dicts; a FieldMap decides which keys matter.
    """
    if path.suffix.lower() == ".csv":
        with path.open(encoding="utf-8", newline="") as f:
            return list(csv.DictReader(f))
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):  # tolerate {"movies": [...]} / {"items": [...]}
        data = data.get("movies") or data.get("items") or []
    return data if isinstance(data, list) else []


# Backwards-compatible alias (a Kinopoisk export is just one kind of record set).
load_export = load_records


def _as_int(value) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _looks_like_const(value) -> bool:
    return bool(value) and str(value).startswith(("tt", "nm"))


def resolve_record(
        session: requests.Session,
        record: dict,
        field_map: FieldMap | None = None,
        *,
        pause: float = 0.5,
) -> Match:
    """Resolve one source record to an IMDb const, guided by a FieldMap.

    If the FieldMap points at a const field that already holds a tt/nm id, that
    id is trusted and no search happens. Otherwise the record's search fields
    drive an IMDb lookup.
    """
    fm = field_map or FieldMap()
    titles = fm.titles(record)
    year_val = record.get(fm.year) if fm.year else None
    kind_val = record.get(fm.kind) if fm.kind else None

    m = Match(
        src_id=record.get(fm.id) if fm.id else None,
        src_title=titles[0] if titles else None,
        src_year=str(year_val) if year_val not in (None, "") else None,
        src_kind=str(kind_val) if kind_val not in (None, "") else None,
        src_rating=_as_int(record.get(fm.rating)) if fm.rating else None,
        src_sentiment=(record.get(fm.sentiment) or None) if fm.sentiment else None,
    )

    # Const already in the record? Trust it, skip the search.
    if fm.const:
        c = record.get(fm.const)
        if _looks_like_const(c):
            m.imdb_const = str(c)
            m.query_used = "(provided)"
            m.score = 1.0
            m.decision = default_decision(m)
            return m

    if not titles:
        m.error = "no search field had a value"
        m.decision = default_decision(m)
        return m

    res = resolve_const(
        session,
        titles=titles,
        year=year_val,
        want_series=fm.want_series(record),
        pause=pause,
        search_fn=search_people if fm.is_person else search_titles,
        noun="name" if fm.is_person else "title",
        transliterate_fallback=fm.is_person and fm.transliterate,
    )
    # Graft the resolved bits onto our source-populated Match.
    m.imdb_const = res.imdb_const
    m.imdb_title = res.imdb_title
    m.imdb_year = res.imdb_year
    m.imdb_kind = res.imdb_kind
    m.query_used = res.query_used
    m.score = res.score
    m.title_score = res.title_score
    m.ambiguous = res.ambiguous
    m.review = res.review
    m.alternatives = res.alternatives
    m.error = res.error
    m.decision = default_decision(m)
    return m


def resolve_records(
        session: requests.Session,
        records: list[dict],
        field_map: FieldMap | None = None,
        *,
        pause: float = 0.5,
) -> list[Match]:
    """Resolve every record to an IMDb const, logging progress to stderr."""
    fm = field_map or FieldMap()
    total = len(records)
    matches: list[Match] = []
    for i, record in enumerate(records, 1):
        res = resolve_record(session, record, fm, pause=pause)
        flag = "?" if res.ambiguous else ("+" if res.matched else "x")
        label = res.src_title or (f"#{res.src_id}" if res.src_id is not None else "?")
        note = f" [{res.review}]" if res.review else ""
        target = res.imdb_const
        if res.imdb_title and res.imdb_title != res.src_title:
            target = f"{res.imdb_const} “{res.imdb_title}”"
        print(
            f"[{i}/{total}] {flag} {label} -> "
            f"{target or res.error} (score={res.score}){note}",
            file=sys.stderr,
        )
        matches.append(res)
        # A trusted const needs no request, so no need to pause after it.
        if i < total and res.query_used != "(provided)":
            time.sleep(pause)
    return matches


# Backwards-compatible alias.
resolve_export = resolve_records


# --------------------------------------------------------------------------- #
# Saving / loading matches
# --------------------------------------------------------------------------- #
# Column order for CSV: put the editable verdict and the review context first so
# the file opens decision-ready in a spreadsheet.
_CSV_ORDER = [
    "decision", "review", "src_title", "src_year", "imdb_const", "imdb_title",
    "imdb_year", "title_score", "score", "imdb_kind", "src_kind", "src_rating",
    "src_sentiment", "src_id", "query_used", "ambiguous", "error", "alternatives",
]


def _alts_str(alts: list[dict]) -> str:
    """Compact one-line rendering of alternatives for a CSV cell."""
    return " | ".join(
        f"{a.get('const')} {a.get('title')} ({a.get('year')}) score={a.get('score')}"
        for a in alts or []
    )


def save_matches(matches: list[Match], path: Path, fmt: str | None = None) -> None:
    fmt = fmt or ("csv" if path.suffix.lower() == ".csv" else "json")
    rows = [asdict(m) for m in matches]
    if fmt == "csv":
        if not rows:
            path.write_text("", encoding="utf-8")
            return
        extra = [k for k in rows[0] if k not in _CSV_ORDER]
        fields = [k for k in _CSV_ORDER if k in rows[0]] + extra
        with path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            for row in rows:
                row = dict(row)
                row["alternatives"] = _alts_str(row.get("alternatives"))
                writer.writerow({k: row.get(k) for k in fields})
    else:
        path.write_text(
            json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def _coerce_csv_row(row: dict) -> dict:
    """Turn a CSV string row back into Match-typed values."""
    out = dict(row)
    for k in ("score", "title_score"):
        if out.get(k) not in (None, ""):
            try:
                out[k] = float(out[k])
            except ValueError:
                out[k] = None
    for k in ("imdb_year", "src_rating"):
        out[k] = _as_int(out.get(k))
    if "ambiguous" in out:
        out["ambiguous"] = str(out.get("ambiguous")).strip().lower() in ("true", "1", "yes")
    out["alternatives"] = []  # not parsed back from the CSV string; unused on import
    # empty strings -> None so they don't override dataclass defaults oddly
    return {k: (None if v == "" else v) for k, v in out.items()}


def load_matches(path: Path) -> list[Match]:
    """Load a matches file written by save_matches (json or csv).

    Reads back your edited `decision` column, so an eyeballed spreadsheet feeds
    straight into `imdb.lists`.
    """
    known = {f for f in Match.__dataclass_fields__}
    out: list[Match] = []
    if path.suffix.lower() == ".csv":
        with path.open(encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                row = _coerce_csv_row(row)
                out.append(Match(**{k: v for k, v in row.items() if k in known}))
        return out
    data = json.loads(path.read_text(encoding="utf-8"))
    for row in data if isinstance(data, list) else []:
        out.append(Match(**{k: v for k, v in row.items() if k in known}))
    return out


# --------------------------------------------------------------------------- #
# Shared CLI helpers: the field map
# --------------------------------------------------------------------------- #
def _split(csv_value: str | None) -> list[str]:
    """'a, b ,c' -> ['a','b','c']."""
    if not csv_value:
        return []
    return [p.strip() for p in csv_value.split(",") if p.strip()]


def add_field_map_args(parser) -> None:
    """Flags that tell the importer which record keys hold what.

    Defaults match a Kinopoisk export, so plain runs need none of these. Pass an
    empty string (e.g. --year-field '') to disable a signal.
    """
    d = FieldMap()
    g = parser.add_argument_group("field mapping (which JSON keys to use)")
    g.add_argument(
        "--entity", choices=["title", "person"], default="title",
        help="what to resolve records to: 'title' -> tt... (default), or "
             "'person' -> nm... (searches by name, defaults --search-fields to "
             "original_name,name and ignores year/kind)",
    )
    g.add_argument(
        "--no-transliterate", action="store_false", dest="transliterate",
        help="(person mode) don't add a transliterated fallback query for a "
             "Cyrillic name that has no Latin form (default: do)",
    )
    g.add_argument(
        "--search-fields", metavar="F,F",
        help="keys to search IMDb by, in priority order "
             f"(default: {','.join(d.search)}; person mode: original_name,name)",
    )
    g.add_argument("--year-field", metavar="F",
                   help=f"key holding the year, for disambiguation (default: {d.year})")
    g.add_argument("--kind-field", metavar="F",
                   help=f"key holding movie/series kind (default: {d.kind})")
    g.add_argument(
        "--const-field", metavar="F",
        help="key that already holds an IMDb const (tt.../nm...); if set and "
             "present, that id is used and no search happens",
    )
    g.add_argument("--id-field", metavar="F",
                   help=f"key holding a source id, for logging (default: {d.id})")
    g.add_argument("--rating-field", metavar="F",
                   help=f"key holding your rating, for --min-rating (default: {d.rating})")
    g.add_argument("--sentiment-field", metavar="F",
                   help="key holding rating sentiment, for --only-positive "
                        f"(default: {d.sentiment})")
    g.add_argument(
        "--series-values", metavar="V,V",
        help="kind values that mean 'series' (extends the built-in set)",
    )


def _picked(value: str | None, default: str | None) -> str | None:
    """CLI override: None -> keep default; '' -> disable; else the value."""
    if value is None:
        return default
    return value or None


def field_map_from_args(args) -> FieldMap:
    """Build a FieldMap from the flags added by add_field_map_args."""
    d = FieldMap()
    entity = getattr(args, "entity", "title")
    person = entity == "person"
    # People resolve to nm... by name; year/kind don't apply (the suggestion API
    # sends neither for names). These are overridable by the explicit flags.
    default_search = ["original_name", "name"] if person else list(d.search)
    default_year = None if person else d.year
    default_kind = None if person else d.kind

    search = _split(args.search_fields) if args.search_fields else default_search
    series_values = set(d.series_values)
    if args.series_values:
        series_values |= {v.lower() for v in _split(args.series_values)}
    return FieldMap(
        search=search,
        year=_picked(args.year_field, default_year),
        kind=_picked(args.kind_field, default_kind),
        const=_picked(args.const_field, d.const),
        rating=_picked(args.rating_field, d.rating),
        sentiment=_picked(args.sentiment_field, d.sentiment),
        id=_picked(args.id_field, d.id),
        series_values=series_values,
        entity=entity,
        transliterate=getattr(args, "transliterate", d.transliterate),
    )
