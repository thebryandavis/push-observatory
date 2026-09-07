"""Example configuration. Real Android package names and public RSS identifiers for a
handful of US news outlets, used as demo data. Replace with your own publisher config.

Android package name -> outlet mapping for the news apps this instance monitors.

CONFIDENCE IS PART OF THE DATA. Every entry carries a `confidence`:

  verified  -- https://play.google.com/store/apps/details?id=<pkg> was fetched
               and the returned app name matched the outlet.
  likely    -- corroborated across multiple third-party sources (APKPure,
               AppBrain, sibling apps sharing a package prefix) but a direct
               Play Store fetch did not succeed.
  unsure    -- plausible but unconfirmed; VERIFY BEFORE TRUSTING.

Anything not `verified` should be re-checked on the device once the emulator
exists. The cheapest check is the one-liner in README ("Confirming package
names on the device"): install the app, then

    adb shell pm list packages -3

and diff against this table. `capture.py` also records every unmapped package
it sees, so a wrong ID here shows up as an "unmapped" row in the dashboard
rather than silently dropping an outlet's alerts.

RSS: several outlets have retired public RSS entirely (AP, Reuters, Bloomberg,
The Athletic, ABC News). That is a real constraint on enrich.py, not an
oversight -- see enrich.py's module docstring.

Which, if any, outlet is treated as a "focus" for comparison purposes is not
decided here -- it is controlled entirely by the FOCUS_OUTLET environment
variable (see recap.py). This table is just outlet metadata.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Outlet:
    name: str
    package: str | None
    confidence: str  # verified | likely | unsure | none
    rss: str | None = None
    rss_confidence: str | None = None
    beat: str = "national"  # national | business | sports | international | local
    note: str = ""
    alternates: tuple[str, ...] = field(default_factory=tuple)


OUTLETS: tuple[Outlet, ...] = (
    Outlet(
        name="ABC News",
        package="com.abc.abcnews",
        confidence="verified",
        rss=None,
        beat="national",
        note="ABC retired its public RSS feeds around 2018, so enrichment for ABC "
             "relies on URL extraction from the alert itself.",
    ),
    Outlet(
        name="CNN",
        package="com.cnn.mobile.android.phone",
        confidence="verified",
        rss="http://rss.cnn.com/rss/cnn_topstories.rss",
        rss_confidence="likely",
        beat="national",
        note="RSS returned HTTP 451 on a direct fetch; likely geo/consent-gated rather "
             "than dead. enrich.py tolerates a feed that will not load.",
    ),
    Outlet(
        name="The New York Times",
        package="com.nytimes.android",
        confidence="verified",
        rss="https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml",
        rss_confidence="likely",
        beat="national",
    ),
    Outlet(
        name="The Washington Post",
        package="com.washingtonpost.android",
        confidence="verified",
        rss="https://feeds.washingtonpost.com/rss/national",
        rss_confidence="likely",
        beat="national",
    ),
    Outlet(
        name="Fox News",
        package="com.foxnews.android",
        confidence="verified",
        rss="https://moxie.foxnews.com/google-publisher/latest.xml",
        rss_confidence="verified",
        beat="national",
    ),
    Outlet(
        name="NBC News",
        package="com.zumobi.msnbc",
        confidence="verified",
        rss="https://feeds.nbcnews.com/nbcnews/public/news",
        rss_confidence="verified",
        beat="national",
        note="The package name is a legacy artifact of the app's Zumobi-built origins. "
             "It really is NBC News.",
    ),
    Outlet(
        name="CBS News",
        package="com.cbsnews.ott",
        confidence="unsure",
        alternates=("com.treemolabs.apps.cbsnews",),
        rss="https://www.cbsnews.com/latest/rss/main",
        rss_confidence="verified",
        beat="national",
        note="CBS ships both a phone app and an OTT/TV app. `com.cbsnews.ott` may be the "
             "TV build, which will not push mobile alerts. If CBS shows zero alerts after "
             "a day, check `adb shell pm list packages -3` for the alternate.",
    ),
    Outlet(
        name="AP News",
        package="mnn.Android",
        confidence="likely",
        alternates=("com.apnews",),
        rss=None,
        beat="national",
        note="Unusual package name, consistently reported across third-party mirrors but "
             "not confirmed by a direct Play Store fetch. `com.apnews` is registered as an "
             "alternate in case the app was repackaged. AP retired public RSS.",
    ),
    Outlet(
        name="Reuters",
        package="com.thomsonreuters.reuters",
        confidence="verified",
        rss=None,
        beat="business",
        note="Reuters discontinued public RSS in June 2020.",
    ),
    Outlet(
        name="BBC News",
        package="bbc.mobile.news.ww",
        confidence="verified",
        rss="http://feeds.bbci.co.uk/news/rss.xml",
        rss_confidence="likely",
        beat="international",
        note="`.ww` is the international ('world wide') build, which is the one available "
             "on a US Play Store account.",
    ),
    Outlet(
        name="The Athletic",
        package="com.theathletic",
        confidence="verified",
        rss=None,
        beat="sports",
        note="Subscription gated. Whether alerts unlock without a subscription is itself a "
             "finding about push as an owned-relationship product.",
    ),
    Outlet(
        name="Axios",
        package=None,
        confidence="none",
        rss=None,
        beat="national",
        note="NO ANDROID APP. Axios sunset its mobile apps in late 2024 in favour of "
             "newsletters and web. Kept in the table deliberately: 'this outlet abandoned "
             "the push channel entirely' is a finding, not a gap.",
    ),
    Outlet(
        name="The Wall Street Journal",
        package="wsj.reader_sp",
        confidence="verified",
        rss="https://feeds.a.dj.com/rss/RSSWorldNews.xml",
        rss_confidence="likely",
        beat="business",
        note="Subscription gated.",
    ),
    Outlet(
        name="USA Today",
        package="com.usatoday.android.news",
        confidence="verified",
        rss="https://rssfeeds.usatoday.com/usatoday-NewsTopStories",
        rss_confidence="likely",
        beat="national",
    ),
    Outlet(
        name="NPR",
        package="org.npr.android.news",
        confidence="verified",
        rss="https://feeds.npr.org/1001/rss.xml",
        rss_confidence="verified",
        beat="national",
        note="NPR One was merged into the main NPR app; there is no separate NPR One to install.",
    ),
    Outlet(
        name="The Guardian",
        package="com.guardian",
        confidence="verified",
        rss="https://www.theguardian.com/world/rss",
        rss_confidence="likely",
        beat="international",
    ),
    Outlet(
        name="ESPN",
        package="com.espn.score_center",
        confidence="verified",
        rss="https://www.espn.com/espn/rss/news",
        rss_confidence="verified",
        beat="sports",
    ),
    Outlet(
        name="Bloomberg",
        package="com.bloomberg.android.plus",
        confidence="verified",
        rss=None,
        beat="business",
        note="Subscription gated. No current official public RSS.",
    ),
    Outlet(
        name="Los Angeles Times",
        package="com.apptivateme.next.la",
        confidence="likely",
        rss="https://www.latimes.com/rss2.0.xml",
        rss_confidence="likely",
        beat="local",
        note="`com.apptivateme.next.*` is the shared package prefix Tribune-lineage papers "
             "use. The local-push question in the brief points here first.",
    ),
)


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------

_BY_PACKAGE: dict[str, Outlet] = {}
for _o in OUTLETS:
    if _o.package:
        _BY_PACKAGE[_o.package] = _o
    for _alt in _o.alternates:
        _BY_PACKAGE.setdefault(_alt, _o)


# Android/system packages we never want cluttering the dataset.
SYSTEM_PACKAGE_PREFIXES = (
    "com.android.",
    "android",
    "com.google.android.gms",
    "com.google.android.apps.wellbeing",
    "com.google.android.googlequicksearchbox",
    "com.google.android.setupwizard",
    "com.google.android.projection",
    "com.google.android.packageinstaller",
    "com.android.vending",
)


def outlet_for(package: str) -> str | None:
    """Return the outlet display name for a package, or None if unmapped."""
    o = _BY_PACKAGE.get(package)
    return o.name if o else None


def outlet_obj(package: str) -> Outlet | None:
    return _BY_PACKAGE.get(package)


def is_system_package(package: str) -> bool:
    return any(package.startswith(p) for p in SYSTEM_PACKAGE_PREFIXES)


def tracked_packages() -> list[str]:
    return sorted(_BY_PACKAGE)


def feeds() -> list[tuple[str, str]]:
    """(outlet_name, rss_url) for every outlet that has a feed."""
    return [(o.name, o.rss) for o in OUTLETS if o.rss]


def install_checklist() -> str:
    """Human-readable install list for the runbook."""
    lines = []
    for o in sorted(OUTLETS, key=lambda x: x.name):
        if not o.package:
            lines.append(f"  [--] {o.name:<24} NO ANDROID APP - {o.note.splitlines()[0]}")
            continue
        flag = {"verified": "  ", "likely": " ?", "unsure": " !"}.get(o.confidence, " ?")
        lines.append(f"  [{flag}] {o.name:<24} {o.package}")
    return "\n".join(lines)


if __name__ == "__main__":
    print("Outlets tracked by the Push Observatory")
    print("  ?  = package id likely but unconfirmed")
    print("  !  = package id UNSURE, verify on device before trusting")
    print()
    print(install_checklist())
    print()
    print(f"{len(_BY_PACKAGE)} package ids, {len(feeds())} RSS feeds")
