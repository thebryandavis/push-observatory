#!/usr/bin/env python3
"""Build the full demo dataset for the Push Observatory POC.

This is the richer, deterministic sibling of ``seed_demo.py``: 30 days across
all 18 tracked outlets, plus the operational rows (capture log, package
snapshot, observation notes) that make the Monitoring and Observations views
non-empty. It exists so the deployed demo shows what the tool does, not a
seven-day smoke test.

EVERY alert row this writes is marked ``synthetic = 1``. Nothing here is a
real captured push notification, and nothing here is attributed to a real
news event as fact -- headlines use generic framings ("Fed holds rates
steady", "Category 3 hurricane makes landfall on Gulf Coast") rather than
specific dates or details that would misrepresent an actual story.

Usage:
    python demo/build_dataset.py                 # writes alerts.db next to this repo
    python demo/build_dataset.py --db /tmp/x.db
    python demo/build_dataset.py --days 30 --seed 20260902
"""

from __future__ import annotations

import argparse
import os
import random
import sys
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import cluster  # noqa: E402
import db  # noqa: E402
import monitoring  # noqa: E402
import packages  # noqa: E402
import seed_demo  # noqa: E402

SYNTHETIC_NOTE = "SYNTHETIC DEMO DATA - not a real captured alert"

# Extra generic event templates, on top of seed_demo.EVENTS, so a 30-day
# dataset does not repeat the same four stories a dozen times each. Every
# headline is a generic framing of a *kind* of story, not a specific claim
# about a real date or outcome.
EXTRA_EVENTS = [
    (
        "regional earthquake",
        {
            "headline": {
                "wire": "Magnitude 6.1 earthquake strikes off the coast",
                "cable": "Strong earthquake rattles coastal region",
                "tabloid": "6.1 QUAKE SHAKES COAST",
                "broadcast": "BREAKING: Magnitude 6.1 earthquake reported offshore",
                "legacy": "Magnitude 6.1 Earthquake Strikes Off the Coast",
                "aggregate": "Earthquake: what to know right now",
                "public": "A magnitude 6.1 earthquake was recorded offshore",
                "international": "6.1 magnitude earthquake hits coastal region",
                "business": "Markets steady after offshore earthquake",
                "local": "No major damage reported after morning quake",
            },
            "body": {
                "wire": "No tsunami warning was issued, officials said.",
                "cable": "Emergency crews are assessing damage reports.",
                "tabloid": "No tsunami warning issued",
                "broadcast": "Officials say a tsunami warning was not issued.",
                "legacy": "Read more at the link.",
                "aggregate": "Here's what officials are saying.",
                "public": "Officials do not expect significant damage.",
                "international": "Authorities ruled out a tsunami threat.",
                "business": "Port operations continued without interruption.",
                "local": "Building inspectors are checking older structures.",
            },
            "outlets": ["AP News", "Reuters", "CNN", "ABC News", "NBC News",
                        "BBC News", "The Guardian", "Los Angeles Times", "USA Today"],
        },
    ),
    (
        "jobs report",
        {
            "headline": {
                "wire": "Employers added 150,000 jobs last month",
                "cable": "Jobs report shows steady hiring",
                "tabloid": "JOBS REPORT BEATS FORECASTS",
                "broadcast": "BREAKING: Jobs report tops expectations",
                "legacy": "Hiring Held Steady Last Month, Data Show",
                "aggregate": "Jobs report: what it means for you",
                "public": "The economy added jobs at a steady pace",
                "international": "US jobs growth holds steady",
                "business": "Payrolls rise 150,000; unemployment holds at 4.1%",
                "local": "What the jobs report means for the local market",
            },
            "body": {
                "wire": "Unemployment held steady at 4.1%.",
                "cable": "Economists had expected a smaller gain.",
                "tabloid": "Beats forecasts",
                "broadcast": "The reading topped economists' forecasts.",
                "legacy": "Read more at the link.",
                "aggregate": "Here's the short version.",
                "public": "Wage growth also ticked higher.",
                "international": "The reading matched analyst expectations.",
                "business": "Futures rose slightly on the print.",
                "local": "Local hiring mirrored the national trend.",
            },
            "outlets": ["AP News", "Reuters", "Bloomberg", "The Wall Street Journal",
                        "CNN", "Fox News", "NBC News", "NPR", "USA Today"],
        },
    ),
    (
        "fed holds rates",
        {
            "headline": {
                "wire": "Fed holds rates steady at policy meeting",
                "cable": "Fed leaves interest rates unchanged",
                "tabloid": "FED STANDS PAT ON RATES",
                "broadcast": "BREAKING: Federal Reserve holds rates steady",
                "legacy": "Fed Holds Rates Steady, Citing Mixed Signals",
                "aggregate": "Fed holds rates: what it means for your wallet",
                "public": "Federal Reserve keeps its benchmark rate unchanged",
                "international": "US Federal Reserve holds rates steady",
                "business": "Fed holds; Powell says future cuts data-dependent",
                "local": "What the Fed's pause means for local mortgages",
            },
            "body": {
                "wire": "The decision was widely expected by economists.",
                "cable": "It's the third straight meeting without a change.",
                "tabloid": "Third straight hold",
                "broadcast": "The central bank cited mixed economic signals.",
                "legacy": "Read more at the link.",
                "aggregate": "Here's how it could affect your rates.",
                "public": "Officials pointed to steady but uneven growth.",
                "international": "Markets had priced in the pause.",
                "business": "Two-year yields were little changed.",
                "local": "Mortgage brokers expect no immediate shift.",
            },
            "outlets": ["Reuters", "Bloomberg", "AP News", "The Wall Street Journal",
                        "CNN", "The New York Times", "Fox News", "ABC News", "NPR"],
        },
    ),
    (
        "severe winter storm",
        {
            "headline": {
                "wire": "Winter storm brings heavy snow to the Northeast",
                "cable": "Major winter storm slams the Northeast",
                "tabloid": "SNOW BOMB HITS THE NORTHEAST",
                "broadcast": "BREAKING: Winter storm dumps heavy snow",
                "legacy": "Winter Storm Brings Heavy Snow to the Northeast",
                "aggregate": "Winter storm: what to know before you commute",
                "public": "A winter storm is bringing heavy snow to the region",
                "international": "Heavy snow blankets the US Northeast",
                "business": "Airlines cancel hundreds of flights amid storm",
                "local": "Schools closed as snow totals climb",
            },
            "body": {
                "wire": "Some areas could see over a foot of snow.",
                "cable": "Travel is treacherous across the region.",
                "tabloid": "Over a foot of snow expected",
                "broadcast": "Officials are urging residents to stay off the roads.",
                "legacy": "Read more at the link.",
                "aggregate": "Here's how to plan your commute.",
                "public": "Several school districts have closed for the day.",
                "international": "Hundreds of flights have been cancelled.",
                "business": "Retailers expect a hit to weekend foot traffic.",
                "local": "Plow crews are working around the clock.",
            },
            "outlets": ["AP News", "Reuters", "CNN", "ABC News", "NBC News", "CBS News",
                        "The Washington Post", "USA Today", "The New York Times"],
        },
    ),
    (
        "championship game",
        {
            "headline": {
                "sports": "Home team wins championship in overtime thriller",
                "wire": "Home team wins title in overtime",
                "cable": "Overtime winner clinches the championship",
                "tabloid": "OT MAGIC WINS THE TITLE",
                "broadcast": "Home team wins championship in overtime",
                "legacy": "Home Team Wins Championship in Overtime",
                "aggregate": "Championship: the play that won it",
                "public": "The home team wins the championship in overtime",
                "international": "Home team claims championship title",
                "business": "Championship win boosts ticket and merch sales",
                "local": "City plans celebration after championship win",
            },
            "body": {
                "sports": "A last-second field goal sealed the win.",
                "wire": "The winning score came with seconds left.",
                "cable": "A dramatic final drive sealed it.",
                "tabloid": "Last-second heroics seal it",
                "broadcast": "The game went to overtime after a late tying score.",
                "legacy": "Read more at the link.",
                "aggregate": "Watch the winning play.",
                "public": "The game went to overtime before the winning score.",
                "international": "The game went to overtime before the winning score.",
                "business": "Local businesses expect a busy celebration weekend.",
                "local": "A celebration parade is being planned downtown.",
            },
            "outlets": ["ESPN", "The Athletic", "AP News", "Los Angeles Times",
                        "Fox News", "USA Today", "CNN", "NBC News"],
        },
    ),
]

BIG_NEWS_EVENT_BONUS = 2   # extra events crammed into a big-news day
BIG_NEWS_VOLUME_MULT = 1.6  # extra routine volume on a big-news day


def generate(days: int, seed_val: int) -> tuple[list[dict], set[str]]:
    """Like seed_demo.generate, but over more days with more event variety
    and a handful of designated "big news" days with extra volume."""
    rng = random.Random(seed_val)
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start_day = (now - timedelta(days=days - 1)).replace(hour=0)

    events_pool = list(seed_demo.EVENTS) + EXTRA_EVENTS
    known = {o.name: o for o in packages.OUTLETS if o.package}

    # Pick ~1 big-news day per 6-8 days, deterministically.
    day_indices = list(range(days))
    n_big = max(1, days // 7)
    big_days = set(rng.sample(day_indices, min(n_big, days)))

    rows: list[dict] = []
    big_day_dates: set[str] = set()

    for d in range(days):
        day = start_day + timedelta(days=d)
        weekend = day.weekday() >= 5
        is_big = d in big_days
        if is_big:
            big_day_dates.add(day.strftime("%Y-%m-%d"))

        n_events = rng.choice([1, 1, 2, 2, 3]) if not weekend else rng.choice([0, 1, 1, 2])
        if is_big:
            n_events += BIG_NEWS_EVENT_BONUS

        used_labels: set[str] = set()
        for _ in range(n_events):
            # Avoid repeating the same story twice in one day where possible.
            choices = [e for e in events_pool if e[0] not in used_labels] or events_pool
            label, spec = rng.choice(choices)
            used_labels.add(label)
            t0 = day + timedelta(
                hours=rng.choice([6, 7, 8, 9, 10, 11, 13, 14, 15, 16, 17, 18, 19, 20]),
                minutes=rng.randrange(0, 60),
            )
            participants = [o for o in spec["outlets"] if o in known]
            rng.shuffle(participants)
            participants.sort(key=lambda o: 0 if seed_demo._style(o) == "wire" else 1)
            lag = 0.0
            for i, outlet in enumerate(participants):
                if i and rng.random() < 0.18:
                    continue
                lag += abs(rng.gauss(4.5, 3.5)) if i else 0
                st = seed_demo._style(outlet)
                rows.append({
                    "outlet": outlet,
                    "title": seed_demo._pick(spec["headline"], st),
                    "body": seed_demo._pick(spec["body"], st),
                    "posted": t0 + timedelta(minutes=lag),
                    "category": "breaking",
                    "event": label,
                })

        for outlet, (per_day, st) in seed_demo.PROFILE.items():
            if outlet not in known:
                continue
            mult = 0.7 if weekend else 1.0
            if is_big:
                mult *= BIG_NEWS_VOLUME_MULT
            target = max(0, int(rng.gauss(per_day * mult, 1.6)))
            pool = seed_demo.ROUTINE.get(st, seed_demo.ROUTINE["wire"])
            for _ in range(target):
                hour = rng.choices(
                    population=list(range(24)),
                    weights=[1, 1, 1, 1, 2, 4, 9, 14, 12, 9, 7, 7,
                             8, 8, 8, 9, 10, 12, 13, 11, 8, 5, 3, 2],
                )[0]
                title, body = rng.choice(pool)
                rows.append({
                    "outlet": outlet,
                    "title": title,
                    "body": body,
                    "posted": day + timedelta(hours=hour, minutes=rng.randrange(60)),
                    "category": "promo" if any(
                        k in title.lower() for k in ("subscribe", "last chance", "deals", "save")
                    ) else "news",
                    "event": None,
                })

    rows = [r for r in rows if r["posted"] <= now]
    rows.sort(key=lambda r: r["posted"])
    return rows, big_day_dates


def seed_alerts(days: int, db_path: str | None, seed_val: int) -> tuple[int, set[str]]:
    db.init_db(db_path)
    rows, big_days = generate(days, seed_val)
    pkg_of = {o.name: o.package for o in packages.OUTLETS}
    n = 0
    with db.connect(db_path) as conn:
        for i, r in enumerate(rows):
            ok = db.insert_alert(
                conn,
                package=pkg_of.get(r["outlet"]) or "synthetic.unknown",
                outlet=r["outlet"],
                title=r["title"],
                body=r["body"],
                posted_at=r["posted"].isoformat(timespec="seconds"),
                captured_at=(r["posted"] + timedelta(seconds=15)).isoformat(timespec="seconds"),
                category=r["category"],
                url=None,
                section="live",
                synthetic=True,
                raw={"synthetic": True, "note": SYNTHETIC_NOTE, "event": r["event"], "seq": i},
            )
            n += 1 if ok else 0
    return n, big_days


def seed_capture_log(db_path: str | None, n_polls: int = 240) -> int:
    """A healthy-looking instrument: 30s cadence, last poll ~1 minute ago,
    mostly-clean history with a couple of transient failures further back
    so the log does not read as suspiciously perfect."""
    db.init_db(db_path)
    now = datetime.now(timezone.utc)
    last_poll = now - timedelta(minutes=1)
    rng = random.Random(20260902)
    rows = []
    for i in range(n_polls):
        ts = last_poll - timedelta(seconds=30 * i)
        # A couple of blips ~2 hours back; everything recent is clean.
        ok = not (60 <= i <= 61 and rng.random() < 0.5)
        parsed = rng.randint(0, 4) if ok else 0
        inserted = rng.randint(0, parsed) if ok else 0
        note = "ok" if ok else "dumpsys timeout"
        rows.append((ts.isoformat(timespec="seconds"), 1 if ok else 0, parsed, inserted, note))
    rows.reverse()  # oldest first, so ids increase with time
    with db.connect(db_path) as conn:
        conn.executemany(
            "INSERT INTO capture_log (ts, ok, parsed, inserted, note) VALUES (?,?,?,?,?)",
            rows,
        )
    return len(rows)


def seed_package_snapshot(db_path: str | None) -> int:
    db.init_db(db_path)
    installed = sorted(o.package for o in packages.OUTLETS if o.package)
    monitoring.store_snapshot(
        installed, db_path,
        note="SYNTHETIC DEMO SNAPSHOT - sample `pm list packages -3` output, all 18 tracked apps installed",
    )
    return len(installed)


# A few neutral, sample observation notes: what an app *does*, not editorial
# judgement about whether that's good or bad. Clearly flagged as demo notes
# via the `author` field, so they are never mistaken for a real observation
# session's findings.
SAMPLE_NOTES: dict[str, list[str]] = {
    "The New York Times": [
        "Follows: auto-enable alerts. On first launch, 'Breaking News' and "
        "'Top Stories' toggles both ship ON; the user never sees an explicit opt-in.",
        "Pre-prompt shown before the OS permission dialog on first launch, with "
        "copy that frames declining as 'maybe later' rather than 'no thanks'.",
        "No frequency control anywhere in Settings > Notifications -- only a "
        "per-category on/off, no 'fewer/more' slider.",
    ],
    "Fox News": [
        "Breaking News toggle ON by default; a separate 'Alerts' category for "
        "opinion-adjacent content also defaults ON.",
        "In-app notification settings are nested three taps deep (Settings > "
        "Notifications > Manage), with no shortcut from the push itself.",
        "Push copy frequently uses all-caps and an exclamation point even for "
        "non-breaking items, consistent with what copy_analysis.py flags.",
    ],
    "The Athletic": [
        "Subscription-gated: the notification permission pre-prompt appears "
        "before the paywall, so a non-subscriber can still opt in to alerts.",
        "Team-following flow is separate from the news-alert toggle -- a user "
        "can follow a team for scores without enabling general breaking alerts.",
    ],
    "USA Today": [
        "'Today's top stories' digest toggle defaults ON alongside a separate "
        "'Deals of the day' promotional category, also defaulting ON.",
        "No visible in-app path to mute promotional pushes without also muting "
        "the daily news digest -- they share one settings row.",
        "OS-level channel names (Settings > Apps > Notifications) do not match "
        "the in-app category names, which makes cross-referencing the two harder.",
    ],
}


def seed_observations(db_path: str | None) -> int:
    db.init_db(db_path)
    pkg_of = {o.name: o.package for o in packages.OUTLETS}
    now = datetime.now(timezone.utc)
    n = 0
    with db.connect(db_path) as conn:
        i = 0
        for outlet, notes in SAMPLE_NOTES.items():
            for note in notes:
                ts = (now - timedelta(days=len(SAMPLE_NOTES) - i, hours=i)).isoformat(timespec="seconds")
                db.add_observation(
                    conn, outlet=outlet, package=pkg_of.get(outlet),
                    note=f"[SAMPLE DEMO NOTE] {note}",
                    author="demo dataset", ts=ts,
                )
                n += 1
                i += 1
    return n


def build(days: int = 30, db_path: str | None = None, seed_val: int = 20260902) -> dict:
    db.init_db(db_path)
    n_alerts, big_days = seed_alerts(days, db_path, seed_val)
    cluster_res = cluster.run(db_path=db_path, include_synthetic=True)
    n_log = seed_capture_log(db_path)
    n_pkg = seed_package_snapshot(db_path)
    n_notes = seed_observations(db_path)
    return {
        "alerts": n_alerts,
        "days": days,
        "big_news_days": sorted(big_days),
        "clusters": cluster_res,
        "capture_log_rows": n_log,
        "packages_snapshot": n_pkg,
        "observation_notes": n_notes,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Build the full Push Observatory demo dataset")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--db")
    ap.add_argument("--seed", type=int, default=20260902)
    ap.add_argument("--purge", action="store_true", help="delete synthetic rows and exit")
    args = ap.parse_args(argv)

    if args.purge:
        n = seed_demo.purge(args.db)
        print(f"deleted {n} synthetic alerts")
        return 0

    res = build(args.days, args.db, args.seed)
    print(f"seeded {res['alerts']} SYNTHETIC alerts across {res['days']} days "
          f"({len(res['big_news_days'])} big-news days)")
    print(f"clustered: {res['clusters']}")
    print(f"capture_log: {res['capture_log_rows']} rows (healthy, last poll ~1 min ago)")
    n_apps = sum(1 for o in packages.OUTLETS if o.package)
    print(f"package_snapshot: {res['packages_snapshot']}/{n_apps} outlets installed")
    print(f"observations: {res['observation_notes']} sample notes")
    print(db.stats(args.db))
    print("\nAll alert rows are flagged synthetic=1. Purge with:")
    print("  python demo/build_dataset.py --purge")
    return 0


if __name__ == "__main__":
    sys.exit(main())
