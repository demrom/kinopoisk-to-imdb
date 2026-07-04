"""Kinopoisk scrapers: a shared core plus sibling scrapers.

- kinopoisk.core          — Movie model, HTTP session/cookies, saving helpers
- kinopoisk.ratings       — /movies/voted-watched/ (your rated movies)
- kinopoisk.content_lists — /mykp/folders/<id>/ (personal content lists)
- kinopoisk.stars         — /mykp/stars/list/type/<id>/ (saved people)

Run a scraper as a module, e.g.:
    python -m kinopoisk.ratings
    python -m kinopoisk.content_lists --all
    python -m kinopoisk.stars --all

Models: `Movie` (below) and `kinopoisk.stars.Person`. Submodules that are
runnable via `python -m` are not imported here (importing them eagerly would
warn under runpy) — import them directly, e.g. `from kinopoisk.stars import Person`.
"""

from .core import Movie

__all__ = ["Movie"]
