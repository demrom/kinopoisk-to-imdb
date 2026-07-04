#!/opt/homebrew/bin/python3.11
"""Extract Kinopoisk / IMDb cookies from the browser via rookiepy.

Meant to be reusable: other scripts (e.g. the kinopoisk.ratings /
kinopoisk.content_lists scrapers) get a ready Cookie string from here — either by
running this as a subprocess or by importing get_cookie_string().

Usage (CLI):
    fetch_cookies.py --site kinopoisk [--browser arc] [-v]
    fetch_cookies.py --site imdb
    fetch_cookies.py --host www.kinopoisk.ru --domain kinopoisk.ru   # manual

Output:
    stdout — a "key=value; key2=value2" string (as in the Cookie header).
    stderr — diagnostics. Exit code 1 on failure.

Feed the result to the scrapers (which also auto-pull cookies themselves, so
this is only needed to pin a specific browser or reuse the string elsewhere):
    export KINOPOISK_COOKIE="$(fetch_cookies.py --site kinopoisk)"
    python -m kinopoisk.ratings

Use it from another script:
    from fetch_cookies import get_cookie_string
    cookie = get_cookie_string("kinopoisk")

Dependencies:
    /opt/homebrew/bin/python3.11 -m pip install rookiepy
"""

from __future__ import annotations

import argparse
import sys
from typing import Callable, Dict, List, Optional, Tuple


# --------------------------------------------------------------------------- #
# Site presets: name -> (host the cookies are for; registrable-domain hint)
# --------------------------------------------------------------------------- #
# host   — the real host requests will later go to; we filter cookies by what
#          the browser would actually send there (see cookie_applies_to_host).
# domain — registrable domain without a leading dot; passed to rookiepy as a hint
#          so it only decrypts/returns the relevant branch of cookies.
SITES: Dict[str, Dict[str, str]] = {
    "kinopoisk": {"host": "www.kinopoisk.ru", "domain": "kinopoisk.ru"},
    "imdb": {"host": "www.imdb.com", "domain": "imdb.com"},
}

# Browsers tried in order (Arc is the author's primary). Missing/unavailable ones
# are skipped: their loader raises, we catch it and move on.
DEFAULT_BROWSERS: List[str] = ["arc", "chrome", "brave", "edge", "safari", "firefox"]


def die(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


# --------------------------------------------------------------------------- #
# Cookie-domain vs host matching
# --------------------------------------------------------------------------- #
def cookie_applies_to_host(cookie_domain: str, host: str) -> bool:
    """Would the browser send this cookie on a request to `host`?

    RFC 6265 rule, accounting for how browser stores write the domain field:
      - domain with a leading dot (".kinopoisk.ru") is a "domain" cookie: matches
        the domain itself and all of its subdomains;
      - domain without a dot ("www.kinopoisk.ru") is host-only: matches the exact
        host only.

    Examples for host="www.kinopoisk.ru":
        .kinopoisk.ru        -> True   (domain cookie, covers www)
        kinopoisk.ru         -> True   (parent domain)
        www.kinopoisk.ru     -> True   (exact match)
        hd.kinopoisk.ru      -> False  (different subdomain, host-only)
        .yandex.ru           -> False  (different site)
    """
    host = host.lower().rstrip(".")
    cd = cookie_domain.lower().strip()
    if cd.startswith("."):
        base = cd[1:]
        return host == base or host.endswith("." + base)
    # No leading dot: either host-only (exact match) or a parent domain for which
    # host is a subdomain.
    return host == cd or host.endswith("." + cd)


# --------------------------------------------------------------------------- #
# Fetch raw cookies from browsers
# --------------------------------------------------------------------------- #
def fetch_raw_cookies(
    domain: str,
    browsers: List[str],
    verbose: bool = False,
) -> Tuple[List[Dict], Optional[str]]:
    """Read raw cookies for `domain` from the first browser that has them.

    Returns (cookies, browser_name). rookiepy is imported here so a missing
    dependency yields a clear error with an install hint.
    """
    try:
        import rookiepy
    except ImportError as e:
        print(f"ERROR: Cannot import rookiepy: {e}", file=sys.stderr)
        if verbose:
            print(f"  Python: {sys.executable} ({sys.version.split()[0]})", file=sys.stderr)
            print(f"  sys.path: {sys.path}", file=sys.stderr)
        print(f"  Fix: {sys.executable} -m pip install rookiepy", file=sys.stderr)
        sys.exit(1)

    last_error: Optional[Exception] = None
    for name in browsers:
        loader: Optional[Callable] = getattr(rookiepy, name, None)
        if loader is None:
            if verbose:
                print(f"[DEBUG] rookiepy has no '{name}', skipping", file=sys.stderr)
            continue
        try:
            cookies = loader(domains=[domain])
        except Exception as e:  # noqa: BLE001 — browser not installed / DB locked / etc.
            if verbose:
                print(f"[DEBUG] {name}: {e}", file=sys.stderr)
            last_error = e
            continue
        if cookies:
            if verbose:
                print(f"[DEBUG] {name}: {len(cookies)} raw cookies", file=sys.stderr)
            return cookies, name
        if verbose:
            print(f"[DEBUG] {name}: no cookies for '{domain}'", file=sys.stderr)

    if last_error is not None:
        raise last_error
    return [], None


# --------------------------------------------------------------------------- #
# Filtering and joining
# --------------------------------------------------------------------------- #
def _specificity(cookie: Dict, host: str) -> Tuple[int, int]:
    """Sort key for name clashes: more specific host and longer path win."""
    domain = (cookie.get("domain") or "").lower().lstrip(".")
    exact_host = 1 if domain == host.lower() else 0
    path_len = len(cookie.get("path") or "")
    return (exact_host, path_len)


def select_cookies(raw: List[Dict], host: str, verbose: bool = False) -> List[Dict]:
    """Keep cookies the browser would send to `host` and drop duplicate names.

    On a name clash (one cookie on the domain, another on a subdomain / other
    path) keep the more specific one, so the Cookie string stays close to what
    the browser actually sends.
    """
    applicable = [c for c in raw if cookie_applies_to_host(c.get("domain", ""), host)]
    removed = len(raw) - len(applicable)
    if verbose and removed:
        print(
            f"[DEBUG] Dropped {removed} cookies not for '{host}'; kept {len(applicable)}",
            file=sys.stderr,
        )

    best: Dict[str, Dict] = {}
    for c in applicable:
        name = c.get("name")
        if not name:
            continue
        prev = best.get(name)
        if prev is None or _specificity(c, host) >= _specificity(prev, host):
            best[name] = c

    dropped_dups = len(applicable) - len(best)
    if verbose and dropped_dups:
        print(f"[DEBUG] Collapsed {dropped_dups} duplicate names", file=sys.stderr)

    return list(best.values())


def dump_raw(raw: List[Dict]) -> None:
    """Debug dump of all raw cookies to stderr (enabled by -v)."""
    print(f"[DEBUG] All {len(raw)} raw cookies before filtering:", file=sys.stderr)
    print(f"  {'#':<4} {'domain':<28} {'path':<16} {'name':<32} value[:32]", file=sys.stderr)
    print(f"  {'-'*4} {'-'*28} {'-'*16} {'-'*32} {'-'*32}", file=sys.stderr)
    for i, c in enumerate(raw, 1):
        print(
            f"  {i:<4} {c.get('domain','?'):<28} {c.get('path','?'):<16}"
            f" {c.get('name','?'):<32} {(c.get('value') or '')[:32]}",
            file=sys.stderr,
        )


# --------------------------------------------------------------------------- #
# Public API for other scripts
# --------------------------------------------------------------------------- #
def resolve_target(
    site: Optional[str] = None,
    host: Optional[str] = None,
    domain: Optional[str] = None,
) -> Tuple[str, str]:
    """Reduce (--site / --host / --domain) to a (host, domain) pair."""
    if site:
        preset = SITES.get(site)
        if preset is None:
            raise ValueError(
                f"Unknown site '{site}'. Available: {', '.join(sorted(SITES))}. "
                "Or pass --host and --domain manually."
            )
        host = host or preset["host"]
        domain = domain or preset["domain"]
    if not host or not domain:
        raise ValueError("Need either --site, or both --host and --domain.")
    return host, domain


def get_cookie_string(
    site: Optional[str] = None,
    *,
    host: Optional[str] = None,
    domain: Optional[str] = None,
    browsers: Optional[List[str]] = None,
    verbose: bool = False,
) -> str:
    """Ready Cookie string ("k=v; k=v") for a site. Raises RuntimeError if empty.

    For importing from other scripts:
        from fetch_cookies import get_cookie_string
        cookie = get_cookie_string("kinopoisk")
    """
    host, domain = resolve_target(site, host, domain)
    raw, _browser = fetch_raw_cookies(domain, browsers or DEFAULT_BROWSERS, verbose)
    if not raw:
        raise RuntimeError(f"No cookies for domain '{domain}' in any browser")
    if verbose:
        dump_raw(raw)
    cookies = select_cookies(raw, host, verbose)
    if not cookies:
        raise RuntimeError(f"No cookies left after filtering for host '{host}'")
    return "; ".join(f"{c['name']}={c['value']}" for c in cookies)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Extract Kinopoisk/IMDb cookies from the browser via rookiepy.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--site",
        choices=sorted(SITES),
        help="site preset (sets host+domain). Or pass --host and --domain manually.",
    )
    p.add_argument("--host", help="host the cookies are for, e.g. www.kinopoisk.ru")
    p.add_argument("--domain", help="registrable-domain hint, e.g. kinopoisk.ru")
    p.add_argument(
        "--browser",
        action="append",
        dest="browsers",
        metavar="NAME",
        help=(
            "browser (arc/chrome/brave/edge/safari/firefox/...); repeatable; "
            f"default order: {', '.join(DEFAULT_BROWSERS)}"
        ),
    )
    p.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="debug: all cookies before/after filtering, env on import error",
    )
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        host, domain = resolve_target(args.site, args.host, args.domain)
    except ValueError as e:
        die(str(e))

    browsers = args.browsers or DEFAULT_BROWSERS

    try:
        raw, browser = fetch_raw_cookies(domain, browsers, args.verbose)
    except Exception as e:  # noqa: BLE001
        die(f"Failed to read cookies: {e}")

    if not raw:
        die(f"No cookies for domain '{domain}' in any browser ({', '.join(browsers)})")

    if args.verbose:
        dump_raw(raw)

    cookies = select_cookies(raw, host, args.verbose)
    if not cookies:
        die(f"No cookies left after filtering for host '{host}'")

    print(
        f"Got {len(cookies)} cookies for {host} from {browser} (from {len(raw)} raw)",
        file=sys.stderr,
    )
    print("; ".join(f"{c['name']}={c['value']}" for c in cookies))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
