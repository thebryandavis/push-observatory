#!/usr/bin/env python3
"""Generate synthetic alert data so the dashboard is demoable before real data exists.

EVERY ROW THIS WRITES IS MARKED `synthetic = 1`.

That flag is load-bearing. The dashboard reads it, refuses to mix synthetic and
real rows in the same chart without saying so, and shows a permanent banner
while any synthetic row is present. `seed_demo.py --purge` removes them. The
whole point of the project is credibility with real data, so a screenshot of
invented numbers presented as findings would be worse than having no dashboard
at all.

The generator models plausible newsroom behaviour so the *shapes* are
realistic even though the content is invented:

  * a morning briefing wave, a midday lull, an evening peak, overnight quiet
  * breaking events that ripple across outlets with staggered lag, where the
    wires (AP, Reuters) usually go first and the subscription apps trail
  * per-outlet volume and copy style drawn from that outlet's real posture
    (wires terse and frequent; tabloid all-caps; NYT restating headlines;
    The Athletic and WSJ sending promotional pushes)

Usage:
  python seed_demo.py                 seed 7 days
  python seed_demo.py --days 14
  python seed_demo.py --purge         delete synthetic rows only
"""

from __future__ import annotations

import argparse
import os
import random
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db  # noqa: E402
import packages  # noqa: E402

SYNTHETIC_NOTE = "SYNTHETIC DEMO DATA - not a real captured alert"

# (outlet, alerts/day, style) -- style drives the copy generator below.
PROFILE = {
    "AP News": (11, "wire"),
    "Reuters": (9, "wire"),
    "CNN": (9, "cable"),
    "Fox News": (10, "tabloid"),
    "ABC News": (6, "broadcast"),
    "NBC News": (7, "broadcast"),
    "CBS News": (6, "broadcast"),
    "The New York Times": (7, "legacy"),
    "The Washington Post": (6, "legacy"),
    "USA Today": (5, "aggregate"),
    "NPR": (4, "public"),
    "BBC News": (6, "international"),
    "The Guardian": (5, "international"),
    "The Wall Street Journal": (5, "business"),
    "Bloomberg": (6, "business"),
    "ESPN": (8, "sports"),
    "The Athletic": (4, "sports"),
    "Los Angeles Times": (4, "local"),
}

# Breaking events: (label, [(outlet, lag_minutes)], per-outlet headline styles)
EVENTS = [
    (
        "supreme court tariff ruling",
        {
            "headline": {
                "wire": "Supreme Court rules 6-3 against emergency tariff authority",
                "cable": "Supreme Court rules on emergency tariff case",
                "tabloid": "SCOTUS DEALS BLOW TO TARIFF PLAN",
                "broadcast": "BREAKING: Supreme Court strikes down emergency tariffs",
                "legacy": "Supreme Court Rejects Emergency Tariffs in 6-3 Ruling",
                "aggregate": "Supreme Court tariff ruling: what it means for prices",
                "public": "Supreme Court limits presidential tariff authority",
                "international": "US Supreme Court strikes down emergency tariffs",
                "business": "Supreme Court voids emergency tariffs; refund fight looms",
                "local": "How the Supreme Court tariff ruling hits California ports",
            },
            "body": {
                "wire": "Justices ruled the president exceeded authority under IEEPA.",
                "cable": "The 6-3 decision limits presidential power over trade.",
                "tabloid": "High court rules 6-3 against emergency trade authority",
                "broadcast": "Justices rule 6-3 that the president exceeded his authority.",
                "legacy": "Read more at the link.",
                "aggregate": "Here's what the ruling means for what you pay.",
                "public": "The decision unwinds duties collected since February.",
                "international": "The ruling unwinds duties collected since February.",
                "business": "Importers may be owed billions in refunds.",
                "local": "Port of LA volumes could shift within weeks.",
            },
            "outlets": ["AP News", "Reuters", "Fox News", "ABC News", "CNN",
                        "NBC News", "The New York Times", "Bloomberg",
                        "The Washington Post", "BBC News", "USA Today", "NPR"],
        },
    ),
    (
        "hurricane landfall",
        {
            "headline": {
                "wire": "Hurricane makes landfall on Gulf Coast as Category 3",
                "cable": "Hurricane slams Gulf Coast as Category 3 storm",
                "tabloid": "MONSTER STORM SLAMS GULF COAST",
                "broadcast": "BREAKING: Hurricane makes landfall as Category 3",
                "legacy": "Hurricane Makes Landfall on the Gulf Coast",
                "aggregate": "Hurricane landfall: what to know right now",
                "public": "Hurricane comes ashore on the Gulf Coast",
                "international": "Category 3 hurricane hits US Gulf Coast",
                "business": "Gulf refineries shut as hurricane makes landfall",
                "local": "Storm surge warnings extended along the coast",
            },
            "body": {
                "wire": "Sustained winds of 120 mph were recorded at landfall.",
                "cable": "Storm surge of up to 12 feet is forecast overnight.",
                "tabloid": "120 MPH winds, 12-foot surge feared",
                "broadcast": "Officials urged residents in low-lying areas to evacuate.",
                "legacy": "Read more at the link.",
                "aggregate": "Here's what to know if you're in the path.",
                "public": "Evacuation orders remain in effect for three parishes.",
                "international": "Hundreds of thousands are without power.",
                "business": "Roughly 12% of US refining capacity is offline.",
                "local": "Local shelters have opened; check the list.",
            },
            "outlets": ["AP News", "Reuters", "CNN", "Fox News", "ABC News",
                        "NBC News", "CBS News", "The Washington Post",
                        "USA Today", "Bloomberg", "Los Angeles Times"],
        },
    ),
    (
        "fed rate decision",
        {
            "headline": {
                "wire": "Fed cuts benchmark rate by a quarter point",
                "cable": "Fed cuts interest rates by a quarter point",
                "tabloid": "FED SLASHES RATES",
                "broadcast": "BREAKING: Federal Reserve cuts interest rates",
                "legacy": "Fed Cuts Rates a Quarter Point, Citing Cooling Labor Market",
                "aggregate": "Fed cuts rates: what it means for your mortgage",
                "public": "Federal Reserve lowers its benchmark rate",
                "international": "US Federal Reserve cuts rates",
                "business": "Fed cuts 25bp; Powell signals data-dependent path",
                "local": "What the Fed's cut means for California housing",
            },
            "body": {
                "wire": "The move was widely expected by economists.",
                "cable": "It's the second cut this year.",
                "tabloid": "Second cut of the year",
                "broadcast": "The central bank cited a cooling labor market.",
                "legacy": "Read more at the link.",
                "aggregate": "Here's how your rates could change.",
                "public": "Officials pointed to slowing job growth.",
                "international": "Markets had priced the cut in fully.",
                "business": "Two-year yields fell 6bp on the statement.",
                "local": "Local mortgage brokers expect little immediate change.",
            },
            "outlets": ["Reuters", "Bloomberg", "AP News", "The Wall Street Journal",
                        "CNBC-like", "CNN", "The New York Times", "Fox News",
                        "ABC News", "NPR"],
        },
    ),
    (
        "playoff result",
        {
            "headline": {
                "sports": "Dodgers walk off in Game 5 to take the series",
                "wire": "Dodgers win Game 5 on a walk-off homer",
                "cable": "Dodgers walk off to advance",
                "tabloid": "WALK-OFF! DODGERS ADVANCE",
                "broadcast": "Dodgers advance on a walk-off home run",
                "legacy": "Dodgers Advance on a Walk-Off Home Run",
                "aggregate": "Dodgers walk off: the play that ended it",
                "public": "Dodgers take the series in five",
                "international": "Dodgers reach next round",
                "business": "Dodgers advance; ratings up 14% year over year",
                "local": "Dodgers walk off to send Los Angeles to the next round",
            },
            "body": {
                "sports": "The three-run shot capped a four-run ninth.",
                "wire": "The home run came with two outs in the ninth.",
                "cable": "A four-run ninth sealed it.",
                "tabloid": "Three-run bomb with two outs",
                "broadcast": "The series ends in five games.",
                "legacy": "Read more at the link.",
                "aggregate": "Watch the highlight.",
                "public": "The series ends in five games.",
                "international": "The series ends in five games.",
                "business": "Ticket resale prices jumped 30% overnight.",
                "local": "Parade plans are already being discussed.",
            },
            "outlets": ["ESPN", "The Athletic", "AP News", "Los Angeles Times",
                        "Fox News", "USA Today", "CNN"],
        },
    ),
]

# Routine, non-breaking pushes. This is the programmatic-messaging evidence the
# brief cares about, so the generator deliberately includes it.
ROUTINE = {
    "wire": [("Morning Wire: the 5 stories to start your day",
              "Curated by AP editors."),
             ("Ukraine talks resume in Geneva", "Negotiators meet for a third day."),
             ("Jobless claims fall to a four-week low", "Claims dropped to 214,000.")],
    "cable": [("5 Things: what you need to know today", "Your morning briefing."),
              ("Analysis: why this week matters", "Our reporters break it down."),
              ("The latest on the wildfire response", "Containment is at 40%.")],
    "tabloid": [("WATCH: the moment that stunned the hearing room",
                 "See the exchange"),
                ("Poll: voters split on the plan", "New numbers out this morning"),
                ("SENATOR ERUPTS AT HEARING", "Watch the exchange"),
                ("\U0001F6A8 ALERT: new details released", "Read now"),
                ("Opinion: the case against the ruling", "Read the argument")],
    "broadcast": [("Good morning. Here's what's happening today.",
                   "Your daily briefing."),
                  ("New polling on the race", "Numbers released this morning."),
                  ("Storm system moves east", "Tap to open."),
                  ("Recall issued for 40,000 vehicles", "Check whether yours is affected."),
                  ("What to watch this weekend", "A look at the week ahead.")],
    # `legacy` is deliberately restatement-heavy: boilerplate bodies that add
    # nothing to the headline. This is the shape the copy-analysis view exists
    # to surface, so the demo has to contain some of it.
    "legacy": [("Your Morning Briefing", "Read more at the link."),
               ("The Daily: this morning's episode is out", "Listen now."),
               ("Senate advances the spending bill", "Read more."),
               ("Cooking: 5 weeknight dinners", "Recipes for the week ahead."),
               ("Israel and Lebanon agree to extend the truce", "Read more at the link.")],
    "aggregate": [("Today's top stories", "Your daily roundup."),
                  ("You haven't read today's briefing", "Catch up in 5 minutes."),
                  ("\U0001F525 Deals of the day", "Handpicked by our editors."),
                  ("Gas prices fall for a third week", "See the map."),
                  ("Subscribe and save 50%", "Limited time offer.")],
    "public": [("Up First: today's episode", "Listen to the news in 12 minutes."),
               ("Consider This: the week in review", "New episode available."),
               ("Planet Money explains the tariff ruling", "New episode out now.")],
    "international": [("Your morning briefing", "The stories shaping the day."),
                      ("First Edition: today's newsletter", "Read it now."),
                      ("Long read: inside the negotiations", "A 12-minute read.")],
    "business": [("Markets open: futures point higher", "S&P futures up 0.4%."),
                 ("Evening Briefing: the day in markets", "Your close-of-day wrap."),
                 ("Subscribe and save 50% on your first year",
                  "Limited time offer for new members.")],
    "sports": [("Your teams: tonight's games", "Personalized for you."),
               ("\u26A1 Last chance: 1 year for $1", "Your trial ends tonight."),
               ("\U0001F3C8 Final: Chiefs 27, Bills 24", "Recap and highlights"),
               ("You haven't opened the app in a while", "Here's what you missed."),
               ("Transfer news: the latest moves", "Updated throughout the day.")],
    "local": [("Your California morning briefing", "Local news to start the day."),
              ("Traffic alert: 405 closure this weekend", "Plan an alternate route."),
              ("Essential California: today's newsletter", "Read it now.")],
}


def _style(outlet: str) -> str:
    return PROFILE.get(outlet, (5, "wire"))[1]


def _pick(mapping: dict, style: str) -> str:
    return mapping.get(style) or mapping.get("wire") or next(iter(mapping.values()))


def generate(days: int, seed: int = 20260902) -> list[dict]:
    rng = random.Random(seed)
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start_day = (now - timedelta(days=days - 1)).replace(hour=0)

    rows: list[dict] = []
    known = {o.name: o for o in packages.OUTLETS if o.package}

    for d in range(days):
        day = start_day + timedelta(days=d)
        weekend = day.weekday() >= 5

        # --- breaking events: 1-3 a day, rippling across outlets ---
        n_events = rng.choice([1, 1, 2, 2, 3]) if not weekend else rng.choice([0, 1, 1, 2])
        for _ in range(n_events):
            label, spec = rng.choice(EVENTS)
            t0 = day + timedelta(
                hours=rng.choice([7, 9, 10, 11, 13, 14, 15, 16, 17, 19]),
                minutes=rng.randrange(0, 60),
            )
            participants = [o for o in spec["outlets"] if o in known]
            rng.shuffle(participants)
            # Wires tend to go first; give them a lag advantage.
            participants.sort(key=lambda o: 0 if _style(o) == "wire" else 1)
            lag = 0.0
            for i, outlet in enumerate(participants):
                if i and rng.random() < 0.18:
                    continue  # not every outlet covers every event
                lag += abs(rng.gauss(4.5, 3.5)) if i else 0
                st = _style(outlet)
                rows.append(
                    {
                        "outlet": outlet,
                        "title": _pick(spec["headline"], st),
                        "body": _pick(spec["body"], st),
                        "posted": t0 + timedelta(minutes=lag),
                        "category": "breaking",
                        "event": label,
                    }
                )

        # --- routine / programmatic pushes ---
        for outlet, (per_day, st) in PROFILE.items():
            if outlet not in known:
                continue
            target = max(0, int(rng.gauss(per_day * (0.7 if weekend else 1.0), 1.6)))
            pool = ROUTINE.get(st, ROUTINE["wire"])
            for _ in range(target):
                # Send-time distribution: morning briefing spike, evening peak,
                # overnight near-silence.
                hour = rng.choices(
                    population=list(range(24)),
                    weights=[1, 1, 1, 1, 2, 4, 9, 14, 12, 9, 7, 7,
                             8, 8, 8, 9, 10, 12, 13, 11, 8, 5, 3, 2],
                )[0]
                title, body = rng.choice(pool)
                rows.append(
                    {
                        "outlet": outlet,
                        "title": title,
                        "body": body,
                        "posted": day + timedelta(hours=hour, minutes=rng.randrange(60)),
                        "category": "promo" if any(
                            k in title.lower()
                            for k in ("subscribe", "last chance", "deals", "save")
                        ) else "news",
                        "event": None,
                    }
                )

    rows = [r for r in rows if r["posted"] <= now]
    rows.sort(key=lambda r: r["posted"])
    return rows


def seed(days: int = 7, db_path: str | None = None, seed_val: int = 20260902) -> int:
    db.init_db(db_path)
    rows = generate(days, seed_val)
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
                # Distinct microsecond-free timestamps; the +i keeps the dedupe
                # hash unique for outlets that repeat a routine headline.
                posted_at=r["posted"].isoformat(timespec="seconds"),
                captured_at=(r["posted"] + timedelta(seconds=15)).isoformat(
                    timespec="seconds"
                ),
                category=r["category"],
                url=None,
                section="live",
                synthetic=True,
                raw={"synthetic": True, "note": SYNTHETIC_NOTE,
                     "event": r["event"], "seq": i},
            )
            n += 1 if ok else 0
    return n


def purge(db_path: str | None = None) -> int:
    db.init_db(db_path)
    with db.connect(db_path) as conn:
        cur = conn.execute("DELETE FROM alerts WHERE synthetic = 1")
        conn.execute("DELETE FROM clusters")
        return cur.rowcount


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Seed synthetic demo data")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--db")
    ap.add_argument("--seed", type=int, default=20260902)
    ap.add_argument("--purge", action="store_true", help="delete synthetic rows and exit")
    args = ap.parse_args(argv)

    if args.purge:
        n = purge(args.db)
        print(f"deleted {n} synthetic alerts")
        return 0

    n = seed(args.days, args.db, args.seed)
    print(f"inserted {n} SYNTHETIC alerts across {args.days} days")
    print(db.stats(args.db))
    print("\nAll rows are flagged synthetic=1. The dashboard will show a banner.")
    print("Remove them with:  python seed_demo.py --purge")
    return 0


if __name__ == "__main__":
    sys.exit(main())
