# Kinopoisk → IMDb migration tool

Export your Kinopoisk data — your **rated movies**, your **content lists**
(favourites, watchlist, anime, …), and your **saved people (stars)** — into
JSON/CSV, then push it the rest of the way: the **`imdb`** package resolves those
exports to real IMDb titles and adds them to one of your IMDb lists.

Cookies are pulled straight from your browser, so there is nothing to copy‑paste
and your real user id is auto‑detected — you never type or commit it.

## What's inside

| Module | Scrapes | Default output |
| --- | --- | --- |
| `kinopoisk.ratings` | your rated movies (`/user/<id>/movies/voted-watched/`) | `out/kinopoisk/ratings.json` |
| `kinopoisk.content_lists` | your content lists (`/mykp/folders/<id>/`) | `out/kinopoisk/content-lists/<id>-<slug>.json` |
| `kinopoisk.stars` | your saved people (`/mykp/stars/list/type/<id>/`) | `out/kinopoisk/stars/<id>-<slug>.json` |
| `fetch_cookies.py` | Kinopoisk / IMDb cookies from the browser | prints to stdout |

`kinopoisk/core.py` holds the shared bits (the `Movie` model, HTTP session &
cookies, movie‑page detail parsing, saving) that the scrapers build on. Saved
people use the `Person` model in `kinopoisk.stars`.

> Kinopoisk calls content lists "folders" in its UI and URLs
> (`/mykp/folders/<id>/`); the module reflects what they actually hold.

The **`imdb`** package goes the other way — from a Kinopoisk export to your IMDb
account:

| Module | Does | Needs login |
| --- | --- | --- |
| `imdb.search` | resolves records to IMDb ids via the public suggestion API | no |
| `imdb.lists` | adds resolved ids to an IMDb list (`AddConstToList`) | yes |
| `imdb.ratings` | sets your 1–10 rating on resolved titles (`rateTitle`) | yes |
| `imdb.watched` | marks resolved titles watched / unwatched (`addWatchedTitle`) | yes |

`imdb/core.py` holds the shared bits: the session/cookies, the suggestion‑search
client, the Kinopoisk→IMDb matcher, and the GraphQL client. See
[Import into IMDb](#import-into-imdb).

## Requirements

- **Python 3.10+** (developed on 3.11)
- A browser **logged into Kinopoisk** — Arc, Chrome, Brave, Edge, Safari or
  Firefox. Cookies are read from it automatically.

## Setup

```bash
git clone https://github.com/demrom/kinopoisk-to-imdb.git
cd kinopoisk-to-imdb

# create and activate a virtualenv
python3.11 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate

# install pinned dependencies
pip install -r requirements.txt
```

Run everything **from the repo root** (so `python -m kinopoisk.*` finds the
package). Re‑activate the venv (`source venv/bin/activate`) in each new shell.

## Authentication (cookies)

Kinopoisk serves these pages to a logged‑in session. By default the scrapers
reuse the cookies from your own browser via
[`rookiepy`](https://pypi.org/project/rookiepy/) — just stay logged into
Kinopoisk in one of the supported browsers. Nothing is stored. The tool doesn't
solve or circumvent captchas: if your session isn't valid, Kinopoisk shows its
usual bot check and the scraper stops.

Check it works:

```bash
python fetch_cookies.py --site kinopoisk   # prints "name=value; name2=value2"
python fetch_cookies.py --site imdb        # same, for IMDb
```

> **macOS:** reading Chrome/Arc cookies may prompt for Keychain access; Safari
> requires giving your terminal **Full Disk Access**.

You can override where cookies come from on any command:

| Flag / env | Source |
| --- | --- |
| `--cookie '<string>'` | inline cookie string |
| `--cookie-file <path>` | a file containing the cookie string |
| `KINOPOISK_COOKIE` env | cookie string from the environment |
| `--browser chrome` | read from a specific browser (repeatable) |
| `--no-browser-cookie` | disable the automatic browser pull |

The same mechanism covers **IMDb** (`fetch_cookies.py --site imdb`, env
`IMDB_COOKIE`) and is used by `imdb.lists` when it writes to your list. Only
`imdb.lists` needs a login — `imdb.search` uses IMDb's public API. See
[Import into IMDb](#import-into-imdb).

## Usage

### Rated movies

```bash
python -m kinopoisk.ratings                     # -> out/kinopoisk/ratings.json
python -m kinopoisk.ratings --user <USER_ID>    # someone else's public list
python -m kinopoisk.ratings --format csv -o out/kinopoisk/ratings.csv
```

Your own user id is auto‑detected from the logged‑in session, so `--user` is only
needed to scrape **someone else's** public list (or set `KINOPOISK_USER`).

### Content lists

```bash
python -m kinopoisk.content_lists --list            # show the catalog: id, count, name
python -m kinopoisk.content_lists --all             # scrape every list
python -m kinopoisk.content_lists --id 6 --id 1102  # only these lists
python -m kinopoisk.content_lists --all --format csv
```

Each list is written to its own file, e.g.
`out/kinopoisk/content-lists/6-любимые-фильмы.json`. Lists are fetched 100 items
per request (`--limit`, the Kinopoisk max) to keep the request count low.

### Saved people (stars)

```bash
python -m kinopoisk.stars --list                # show types: id, count, name
python -m kinopoisk.stars --all                 # scrape every type
python -m kinopoisk.stars --type 2 --type 4     # 2=Актёры, 4=Режиссёры
```

Each type (Актёры, Актрисы, Режиссёры, favourites, …) is written to
`out/kinopoisk/stars/<id>-<slug>.json`, one person per record: `kp_id`, `name`,
`original_name`, `url`, `birth_date`, `photo`.

These are **people**, so resolve them to IMDb name pages (`nm…`) with
`imdb.search --entity person` (see [Resolve people](#resolve-people-to-imdb-nm)),
not to titles. IMDb indexes names by their original (Latin) spelling, so a person
with only a Cyrillic `name` and no `original_name` may come back `unmatched`.

### Offline (no network, no cookies)

Already saved a listing page from the browser? Parse it directly:

```bash
python -m kinopoisk.ratings --from-file page1.html
python -m kinopoisk.ratings --html-dir saved_pages/
```

## Output

```
out/kinopoisk/
├── ratings.json
├── content-lists/
│   ├── 6-любимые-фильмы.json
│   ├── 1-избранное.json
│   └── ...
└── stars/
    ├── 2-актёры.json
    ├── 3-актрисы.json
    └── ...
```

Each file is a JSON list (or CSV) of movie objects. Common fields:

`kp_id`, `kind` (`film`/`series`), `url`, `title`, `year`, `genre`,
`user_rating`, `user_rating_sentiment`, `poster`. Content‑list cards also carry
`original_title`, `countries`, `directors`, `actors`, `duration_min`, plus
`list_id`, `list_name`, `list_position` and `added_at`.

`out/` is git‑ignored — it is your personal data and never committed.

## Import into IMDb

Two steps, on purpose: **find** the titles first, **import** them second, so you
can eyeball the matches before anything touches your account.

```
kinopoisk export ──▶ imdb.search ──▶ matches.json ──▶ imdb.lists ──▶ IMDb list
     (JSON)          (public API)     (+ decisions)    (your cookies)
```

### Full example (start to finish)

**Before anything, log into both Kinopoisk and IMDb in your browser** (Arc,
Chrome, Brave, Edge, Safari or Firefox) — every step reuses those cookies
automatically, so there's nothing to copy‑paste. Then run everything from the
repo root. This takes one Kinopoisk content list all the way into an IMDb list
(`12345678` / `ls123456789` are placeholders — use your own):

```bash
# 0. export the Kinopoisk content list (writes out/kinopoisk/content-lists/<id>-<slug>.json)
python -m kinopoisk.content_lists --id 12345678

# 1. resolve it to IMDb ids — public API, no login
python -m imdb.search \
    out/kinopoisk/content-lists/12345678-my-list.json \
    -o out/imdb/from-kinopoisk/content-lists/12345678-my-list.json

# 2. review: open the file (and the <out>.review.json worklist) and fix any
#    `review` / `unmatched` rows — set decision to accept/reject, or paste the
#    right imdb_const into an unmatched row and set decision=accept.

# 3. dry run against your list, then import for real (ls… is from the list URL)
python -m imdb.lists \
    --from-matches out/imdb/from-kinopoisk/content-lists/12345678-my-list.json \
    --list-id ls123456789 --dry-run
python -m imdb.lists \
    --from-matches out/imdb/from-kinopoisk/content-lists/12345678-my-list.json \
    --list-id ls123456789
```

Re‑running is safe — titles already in the list are detected and skipped. The
steps below break down each command.

### 1. Resolve to IMDb ids — `imdb.search`

Reads a Kinopoisk export (or any JSON list) and looks each title up on IMDb's
**public** suggestion API — no login needed:

```bash
python -m imdb.search \
    out/kinopoisk/content-lists/12345678-my-list.json \
    -o out/imdb/from-kinopoisk/content-lists/12345678-my-list.json
```

The output mirrors the Kinopoisk tree under `out/imdb/from-kinopoisk/` — the file
keeps Kinopoisk's `<id>-<slug>` name, so the `from-kinopoisk` segment makes the
origin obvious. Every row gets an auto‑filled **`decision`**:

| `decision` | meaning |
| --- | --- |
| `accept` | clean match — imported by default |
| `review` | something looks off (title didn't really match, year off, or a close alternative exists) — skipped until you decide |
| `unmatched` | nothing found — fix `imdb_const` by hand or drop the row |

`review` / `unmatched` rows are also written to a focused `<out>.review.json`
worklist. Each match records **why** it was flagged (`review`), a `title_score`
(how well the title matched), the resolved `imdb_const` / `imdb_title` /
`imdb_year`, and a few `alternatives`. Open the file and change `review` →
`accept` (or `reject`) where needed.

Matching prefers the **original title**, then the localized one, and scores by
title similarity + year + movie/series kind — so e.g. *House of Cards* resolves
to the 2013 US series rather than the 1990 UK one.

### 2. Add to an IMDb list — `imdb.lists`

Adds the `accept` rows to a list via the `AddConstToList` GraphQL mutation. This
writes to **your account**, so you must be **logged into IMDb** in your browser —
the cookies are pulled from there via `fetch_cookies` (same as the scrapers).
Verify they're readable first:

```bash
python fetch_cookies.py --site imdb    # should print a cookie string, not an error
```

The cookie‑override flags from [Authentication](#authentication-cookies) apply
here too (`--cookie`, `--cookie-file`, `--browser`, `--no-browser-cookie`); the
env var is `IMDB_COOKIE`. Find the list id in its URL: `imdb.com/list/ls123456789/`
→ `ls123456789` (or set `IMDB_LIST_ID`).

```bash
# dry run first — see exactly what would be added, nothing is sent
python -m imdb.lists \
    --from-matches out/imdb/from-kinopoisk/content-lists/12345678-my-list.json \
    --list-id ls123456789 --dry-run

# for real; only titles you rated 7+ on Kinopoisk
python -m imdb.lists --from-matches <file> --list-id ls123456789 --min-rating 7
```

Only `decision=accept` rows are added; `review` / `reject` / `unmatched` are
skipped (`--include-flagged` adds `review` rows too). Filters: `--min-rating N`,
`--only-positive`, `--limit N`. Titles already in the list are detected and
skipped, and a bad/expired cookie aborts fast instead of hammering the API.

### 3. Transfer your ratings — `imdb.ratings`

Adding a title to a list doesn't rate it. To carry over the **score you gave on
Kinopoisk**, `imdb.ratings` sets your personal IMDb rating on each resolved title
via the `rateTitle` mutation. Both sites rate 1–10, so the value transfers as-is.
Same reviewable matches file, same `decision` gating and cookies as `imdb.lists`:

```bash
# resolve your rated movies first (writes out/imdb/from-kinopoisk/ratings.json)
python -m imdb.search out/kinopoisk/ratings.json -o out/imdb/from-kinopoisk/ratings.json

# dry run — see exactly what would be rated, nothing is sent
python -m imdb.ratings --from-matches out/imdb/from-kinopoisk/ratings.json --dry-run

# for real; only movies you rated 8+
python -m imdb.ratings --from-matches out/imdb/from-kinopoisk/ratings.json --min-rating 8

# undo: clear the ratings this file set (via deleteTitleRating)
python -m imdb.ratings --from-matches out/imdb/from-kinopoisk/ratings.json --delete
```

Only `decision=accept` rows carrying a 1–10 rating are set (`--include-flagged`
also rates `review` rows). `rateTitle` is an upsert, so re-running is safe.
Filters `--min-rating N`, `--only-positive`, `--limit N` apply as in `imdb.lists`.

> **Review first.** A Kinopoisk `ratings.json` export has no original title, so
> its matches resolve mostly to `review` — eyeball them before rating, since a
> wrong rating has to be undone one title at a time (that's what `--delete` is
> for).

### 4. Mark titles watched — `imdb.watched`

To reflect what you've **watched** (e.g. a Kinopoisk "Просмотренные" list),
`imdb.watched` marks each resolved title watched via `addWatchedTitle`. Same
matches file, `decision` gating and cookies as above:

```bash
python -m imdb.watched --from-matches matches.json --dry-run
python -m imdb.watched --from-matches matches.json                 # mark watched
python -m imdb.watched --from-matches matches.json --unwatch       # undo
```

Filters `--min-rating N`, `--only-positive`, `--limit N` and `--include-flagged`
apply as elsewhere. **Rating a title already marks it watched**, so if you ran
`imdb.ratings` those are covered — `imdb.watched` is for titles you watched but
didn't rate.

If you're adding to a list anyway, `imdb.lists --mark-watched` does both in one
pass (add to the list **and** mark watched):

```bash
python -m imdb.lists --from-matches matches.json --list-id ls123456789 --mark-watched
```

### Any JSON, not just Kinopoisk

Point the field map at your own keys — the defaults match a Kinopoisk export:

```bash
python -m imdb.search items.json \
    --search-fields name,localName --year-field releaseYear --kind-field type

# already have the IMDb id in the record? trust it, skip the search
python -m imdb.search items.json --const-field imdb_id
```

Keys: `--search-fields` (priority list of fields to search by), `--year-field`,
`--kind-field`, `--const-field`, `--id-field`, `--rating-field`,
`--sentiment-field`, `--series-values`, `--entity`.

### Resolve people to IMDb (nm…)

By default `imdb.search` resolves records to **titles** (`tt…`). Pass
`--entity person` to resolve **people** to IMDb name pages (`nm…`) instead — it
searches by name and scores on name similarity (year/kind don't apply). In this
mode `--search-fields` defaults to `original_name,name`, so a Kinopoisk stars
export works out of the box:

```bash
python -m imdb.search out/kinopoisk/stars/2-актёры.json --entity person \
    -o out/imdb/from-kinopoisk/stars/2-актёры.json

# one-off lookup
python -m imdb.search --query "Leonardo DiCaprio" --entity person
```

The output is the same reviewable matches file as for titles (`decision` /
`review` / `unmatched`, plus `imdb_const` holding the `nm…`). Note that IMDb
lists hold titles, so `imdb.lists` is for `tt…` matches, not resolved people.

**Cyrillic names.** IMDb indexes names by their original (Latin) spelling, so a
Cyrillic query alone finds nothing. When a record has no Latin form at all
(e.g. Kinopoisk left `original_name` empty), person mode automatically adds a
**transliterated** fallback query — `Кирилл Серебренников` → `Kirill
Serebrennikov` → `nm1970598`. Transliteration is a best guess (IMDb spellings
vary, and a namesake can slip in), so these matches are always flagged
`review` with the transliterated query recorded — confirm them before importing.
Pass `--no-transliterate` to turn this off (Cyrillic-only names then stay
`unmatched`).

## Environment variables

| Variable | Purpose |
| --- | --- |
| `KINOPOISK_COOKIE` | cookie string (overrides the browser pull) |
| `KINOPOISK_USER` | user id for `kinopoisk.ratings` (overrides auto‑detect) |
| `KINOPOISK_UA` | custom `User-Agent` |
| `IMDB_COOKIE` | IMDb cookie string for `imdb.lists` (overrides the browser pull) |
| `IMDB_LIST_ID` | default target list id for `imdb.lists` (used when `--list-id` is omitted) |
| `IMDB_UA` | custom `User-Agent` for IMDb requests |

## Notes

- Be gentle: keep the request rate low. Requests reuse your browser's own headers
  and pause `--pause` seconds (default `2.0`) between hits; if Kinopoisk returns a
  captcha the scraper stops rather than hammering it.
- Import into a fresh shell? Re‑run `source venv/bin/activate` first.

## Disclaimer

A personal **data‑portability** tool for moving **your own** data between your
own accounts — not a bulk‑scraping, data‑mining or data‑resale tool. Everything
it does, you could do by hand in your own browser.

**Kinopoisk (export).** It reads only what your own logged‑in session can already
see (your ratings, lists and saved people), reuses your existing browser cookies,
rate‑limits its requests, and does not solve or circumvent captchas. Exported
data stays local — the `out/` directory is git‑ignored and never published.

**IMDb (import).** It uses IMDb's public suggestion API to look up titles and its
GraphQL API to add titles to **your own** list, authenticated with your own
browser cookies. Use is strictly personal and non‑commercial; IMDb data retrieved
this way is not redistributed and is used only to build your own list.

Use it in accordance with Kinopoisk's / Yandex's and IMDb's / Amazon's Terms of
Service and applicable law; automated access may be restricted and could lead to
rate‑limiting or an account block. Provided **as‑is, without warranty**, and
**not affiliated with, endorsed by, or connected to Kinopoisk, Yandex, IMDb or
Amazon**. You are responsible for how you use it.
