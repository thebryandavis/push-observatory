#!/usr/bin/env python3
"""Match captured alerts to canonical stories via outlet RSS feeds.

WHAT THIS CAN AND CANNOT DO -- read before trusting the output
--------------------------------------------------------------
Enrichment is genuinely partial, and the reason is structural, not a bug:

  * Five of the nineteen example outlets have retired public RSS entirely --
    AP, Reuters, Bloomberg, The Athletic, and ABC News. For those, no feed
    match is possible and enrichment falls back to whatever URL the alert
    carried.
  * Several feeds are top-stories only, so a push about a mid-tier story will
    have no feed entry even for outlets that do publish RSS.
  * Feeds are polled now, but the alert fired earlier. A feed that holds only
    the last 20 items will have rotated past an older alert.

So `matched` is a floor, not a measurement of coverage. Nothing downstream
should treat "no canonical URL" as "not a real story". The dashboard reports
match rate per outlet so this stays visible instead of quietly skewing things.

Matching is title similarity (reusing cluster.py's TF-IDF + entity scorer)
between the alert and each feed entry, restricted to entries published within
`--window` hours of the alert.

Usage
-----
  python enrich.py                  enrich un-enriched alerts
  python enrich.py --all            re-enrich everything
  python enrich.py --offline        skip network, only extract embedded URLs
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db  # noqa: E402
import packages  # noqa: E402
from cluster import TfidfBackend  # noqa: E402

USER_AGENT = "PushObservatory/1.0 (personal research; +https://github.com/)"
DEFAULT_MATCH_THRESHOLD = 0.42
DEFAULT_WINDOW_HOURS = 12

_URL_RE = re.compile(r"https?://[^\s\"'<>)\]]+")


@dataclass
class FeedItem:
    title: str
    link: str
    published: datetime | None


# ---------------------------------------------------------------------------
# Feed fetching
# ---------------------------------------------------------------------------

def fetch_feed(url: str, timeout: int = 20) -> str | None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as e:
        print(f"  ! feed unavailable ({type(e).__name__}: {e}) {url}", file=sys.stderr)
        return None


def _text(el, *names) -> str:
    for n in names:
        found = el.find(n)
        if found is not None and found.text:
            return found.text.strip()
    return ""


def _parse_date(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)  # RSS: RFC 822
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))  # Atom: ISO 8601
        except ValueError:
            return None
    if dt is not None and dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def parse_feed(xml_text: str) -> list[FeedItem]:
    """Parse RSS 2.0 or Atom. Returns [] on anything unparseable."""
    if not xml_text:
        return []
    try:
        root = ET.fromstring(xml_text.strip())
    except ET.ParseError:
        return []

    items: list[FeedItem] = []
    ns = {"atom": "http://www.w3.org/2005/Atom"}

    for item in root.iter():
        tag = item.tag.split("}")[-1]
        if tag == "item":  # RSS
            title = _text(item, "title")
            link = _text(item, "link", "guid")
            pub = _parse_date(_text(item, "pubDate", "date"))
        elif tag == "entry":  # Atom
            title = _text(item, "atom:title") or _text(item, "{http://www.w3.org/2005/Atom}title")
            link_el = item.find("{http://www.w3.org/2005/Atom}link")
            link = link_el.get("href", "") if link_el is not None else ""
            pub = _parse_date(
                _text(item, "{http://www.w3.org/2005/Atom}published",
                      "{http://www.w3.org/2005/Atom}updated")
            )
        else:
            continue
        if title:
            items.append(FeedItem(title=title, link=link, published=pub))
    return items


def load_all_feeds(offline: bool = False) -> dict[str, list[FeedItem]]:
    out: dict[str, list[FeedItem]] = {}
    if offline:
        return out
    for outlet, url in packages.feeds():
        raw = fetch_feed(url)
        items = parse_feed(raw) if raw else []
        out[outlet] = items
        print(f"  {outlet:<24} {len(items):>3} items  {url}")
    return out


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def best_match(
    alert_title: str,
    alert_time: datetime | None,
    items: list[FeedItem],
    threshold: float = DEFAULT_MATCH_THRESHOLD,
    window_hours: int = DEFAULT_WINDOW_HOURS,
) -> tuple[FeedItem | None, float]:
    """Highest-scoring feed item above `threshold`, within the time window."""
    if not items or not alert_title:
        return None, 0.0

    window = timedelta(hours=window_hours)
    candidates = []
    for it in items:
        if alert_time and it.published:
            if abs(it.published - alert_time) > window:
                continue
        candidates.append(it)
    if not candidates:
        return None, 0.0

    backend = TfidfBackend()
    backend.prepare([alert_title] + [c.title for c in candidates])

    best, best_score = None, 0.0
    for i, c in enumerate(candidates, start=1):
        s = backend.similarity(0, i)
        if s > best_score:
            best, best_score = c, s
    return (best, best_score) if best_score >= threshold else (None, best_score)


def extract_url(raw_json: str | None, title: str, body: str) -> str | None:
    """Fallback when no feed match: any URL the alert itself carried."""
    for blob in (body or "", title or "", raw_json or ""):
        m = _URL_RE.search(blob)
        if m:
            return m.group(0).rstrip(".,;)\"'")
    return None


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def run(db_path: str | None = None, redo: bool = False, offline: bool = False,
        threshold: float = DEFAULT_MATCH_THRESHOLD,
        window_hours: int = DEFAULT_WINDOW_HOURS) -> dict:
    db.init_db(db_path)

    print("Loading feeds...")
    feeds = load_all_feeds(offline=offline)
    missing = [o.name for o in packages.OUTLETS if o.package and not o.rss]
    if missing:
        print(f"\nNo public RSS for: {', '.join(missing)} -- URL fallback only.\n")

    sql = "SELECT id, outlet, title, body, posted_at, url, raw FROM alerts"
    if not redo:
        sql += " WHERE url IS NULL OR url = ''"

    stats = {"considered": 0, "matched": 0, "url_fallback": 0}
    per_outlet: dict[str, list[int]] = {}

    with db.connect(db_path) as conn:
        rows = conn.execute(sql).fetchall()
        for r in rows:
            stats["considered"] += 1
            outlet = r["outlet"] or ""
            counts = per_outlet.setdefault(outlet, [0, 0])
            counts[1] += 1

            ts = None
            if r["posted_at"]:
                try:
                    ts = datetime.fromisoformat(r["posted_at"].replace("Z", "+00:00"))
                except ValueError:
                    ts = None

            item, score = best_match(
                r["title"] or "", ts, feeds.get(outlet, []), threshold, window_hours
            )
            if item and item.link:
                conn.execute("UPDATE alerts SET url=? WHERE id=?", (item.link, r["id"]))
                stats["matched"] += 1
                counts[0] += 1
                continue

            fallback = extract_url(r["raw"], r["title"] or "", r["body"] or "")
            if fallback and not r["url"]:
                conn.execute("UPDATE alerts SET url=? WHERE id=?", (fallback, r["id"]))
                stats["url_fallback"] += 1

    print(f"\nconsidered={stats['considered']} feed-matched={stats['matched']} "
          f"url-fallback={stats['url_fallback']}")
    print("\nMatch rate by outlet (feed matches / alerts considered):")
    for outlet, (m, n) in sorted(per_outlet.items()):
        has_feed = any(o.name == outlet and o.rss for o in packages.OUTLETS)
        flag = "" if has_feed else "  (no public RSS)"
        print(f"  {outlet:<24} {m:>4}/{n:<4}{flag}")
    return stats


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Match alerts to canonical stories via RSS")
    ap.add_argument("--db")
    ap.add_argument("--all", dest="redo", action="store_true", help="re-enrich everything")
    ap.add_argument("--offline", action="store_true", help="no network; URL extraction only")
    ap.add_argument("--threshold", type=float, default=DEFAULT_MATCH_THRESHOLD)
    ap.add_argument("--window", type=int, default=DEFAULT_WINDOW_HOURS, help="hours")
    args = ap.parse_args(argv)
    run(args.db, redo=args.redo, offline=args.offline,
        threshold=args.threshold, window_hours=args.window)
    return 0


if __name__ == "__main__":
    sys.exit(main())
