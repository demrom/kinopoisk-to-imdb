"""IMDb importer: take a Kinopoisk export and push it to IMDb.

Two thin siblings on top of a shared core:

- imdb.core    — HTTP session/cookies, models, the suggestion-search client,
                 Kinopoisk->IMDb matching, and the GraphQL client.
- imdb.search  — resolve a Kinopoisk export into IMDb consts (tt...). Public
                 endpoint, no login needed. Writes a reviewable matches file.
- imdb.lists   — add resolved consts to an IMDb list via the AddConstToList
                 GraphQL mutation. Needs your IMDb cookies.

Run a step as a module, e.g.:
    python -m imdb.search movies.json -o matches.json
    python -m imdb.lists --from-matches matches.json --list-id ls123456789
"""

from .core import ImdbTitle, Match

__all__ = ["ImdbTitle", "Match"]
