"""Tests for storage, ingest, copy analysis and the collector decoder.

No emulator, no network, no API key.
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import capture  # noqa: E402
import collector  # noqa: E402
import copy_analysis as ca  # noqa: E402
import db  # noqa: E402
import packages  # noqa: E402
import seed_demo  # noqa: E402
from parser import parse_dumpsys  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"


@pytest.fixture()
def dbfile(tmp_path):
    p = str(tmp_path / "t.db")
    db.init_db(p)
    return p


# --------------------------------------------------------------------------
# Storage + dedupe
# --------------------------------------------------------------------------

def test_insert_and_dedupe(dbfile):
    with db.connect(dbfile) as c:
        args = dict(package="com.abc.abcnews", outlet="ABC News", title="T",
                    body="B", posted_at="2026-09-02T14:57:00+00:00")
        assert db.insert_alert(c, **args) is True
        assert db.insert_alert(c, **args) is False, "same (pkg,title,time) must dedupe"
        # A different post time is a different alert.
        assert db.insert_alert(c, **{**args, "posted_at": "2026-09-02T15:00:00+00:00"})
    assert db.stats(dbfile)["alerts"] == 2


def test_dedupe_hash_matches_the_brief_tuple():
    a = db.dedupe_hash("p", "t", "2026-01-01")
    assert a == db.dedupe_hash("p", "t", "2026-01-01")
    assert a != db.dedupe_hash("p", "t2", "2026-01-01")
    assert a != db.dedupe_hash("p2", "t", "2026-01-01")
    assert a != db.dedupe_hash("p", "t", "2026-01-02")


def test_init_db_is_idempotent(dbfile):
    db.init_db(dbfile)
    db.init_db(dbfile)
    assert db.stats(dbfile)["alerts"] == 0


# --------------------------------------------------------------------------
# capture ingest
# --------------------------------------------------------------------------

def test_ingest_from_fixture_writes_expected_outlets(dbfile):
    recs = parse_dumpsys((FIXTURES / "dumpsys_notification_sample.txt").read_text())
    counts = capture.ingest(recs, db_path=dbfile)
    assert counts["inserted"] == 5
    with db.connect(dbfile) as c:
        outlets = {r["outlet"] for r in c.execute("SELECT outlet FROM alerts")}
    assert outlets == {"CNN", "ABC News", "The New York Times",
                       "The Athletic", "Fox News"}


def test_ingest_is_idempotent(dbfile):
    recs = parse_dumpsys((FIXTURES / "dumpsys_notification_sample.txt").read_text())
    capture.ingest(recs, db_path=dbfile)
    second = capture.ingest(recs, db_path=dbfile)
    assert second["inserted"] == 0, "re-polling the same shade must insert nothing"


def test_ingest_skips_system_packages(dbfile):
    recs = parse_dumpsys((FIXTURES / "dumpsys_malformed.txt").read_text())
    counts = capture.ingest(recs, db_path=dbfile)
    assert counts["skipped_system"] >= 0
    with db.connect(dbfile) as c:
        pkgs = {r["package"] for r in c.execute("SELECT package FROM alerts")}
    assert not any(p.startswith("com.android.") for p in pkgs)


def test_ingest_dry_run_writes_nothing(dbfile):
    recs = parse_dumpsys((FIXTURES / "dumpsys_notification_sample.txt").read_text())
    capture.ingest(recs, dry_run=True, db_path=dbfile)
    assert db.stats(dbfile)["alerts"] == 0


def test_raw_json_is_stored_and_valid(dbfile):
    recs = parse_dumpsys((FIXTURES / "dumpsys_notification_sample.txt").read_text())
    capture.ingest(recs, db_path=dbfile)
    with db.connect(dbfile) as c:
        raw = c.execute("SELECT raw FROM alerts LIMIT 1").fetchone()["raw"]
    parsed = json.loads(raw)
    assert parsed["package"] and "extras" in parsed


def test_find_adb_returns_none_when_absent(monkeypatch):
    monkeypatch.setattr(capture.shutil, "which", lambda _: None)
    monkeypatch.setattr(capture.os.path, "isfile", lambda _: False)
    monkeypatch.delenv("ANDROID_SDK_ROOT", raising=False)
    monkeypatch.delenv("ANDROID_HOME", raising=False)
    assert capture.find_adb() is None


# --------------------------------------------------------------------------
# packages
# --------------------------------------------------------------------------

def test_package_mapping():
    assert packages.outlet_for("com.abc.abcnews") == "ABC News"
    assert packages.outlet_for("com.nytimes.android") == "The New York Times"
    assert packages.outlet_for("com.totally.unknown") is None


def test_alternates_resolve_to_the_same_outlet():
    assert packages.outlet_for("com.treemolabs.apps.cbsnews") == "CBS News"


def test_system_packages_are_recognised():
    assert packages.is_system_package("com.android.systemui")
    assert packages.is_system_package("com.google.android.gms")
    assert not packages.is_system_package("com.cnn.mobile.android.phone")


def test_axios_is_recorded_as_having_no_app():
    axios = next(o for o in packages.OUTLETS if o.name == "Axios")
    assert axios.package is None
    assert axios.confidence == "none"


def test_every_outlet_declares_a_confidence():
    valid = {"verified", "likely", "unsure", "none"}
    for o in packages.OUTLETS:
        assert o.confidence in valid, o.name


# --------------------------------------------------------------------------
# copy analysis
# --------------------------------------------------------------------------

@pytest.mark.parametrize("title,body,expected", [
    ("Supreme Court strikes down tariffs", "Read more at nytimes.com", "restatement"),
    ("Supreme Court strikes down tariffs", "", "restatement"),
    ("Supreme Court strikes down tariffs", "Tap to open", "restatement"),
    ("Supreme Court strikes down tariffs",
     "Justices ruled 6-3 that the president exceeded authority under IEEPA.", "adds_detail"),
    ("Fed cuts rates", "Here's what it means for your mortgage.", "hook"),
])
def test_framing_classification(title, body, expected):
    assert ca.classify_framing(title, body) == expected


@pytest.mark.parametrize("title,body,expected", [
    ("BREAKING: Court rules", "x", "breaking"),
    ("Last chance: 1 year for $1", "Your trial ends tonight.", "promotional"),
    ("Subscribe and save 50%", "Limited time offer.", "promotional"),
    ("Senate advances spending bill", "The vote was 60-38.", "standard"),
])
def test_kind_classification(title, body, expected):
    assert ca.classify_kind(title, body) == expected


def test_promo_category_overrides_wording():
    assert ca.classify_kind("Your teams tonight", "Games starting soon", "marketing") \
        == "promotional"


def test_emoji_detection():
    assert ca.has_emoji("⚡ Last chance")
    assert ca.has_emoji("🚨 ALERT")
    assert not ca.has_emoji("Plain text headline")


def test_shouty_detection():
    a = ca.analyze_alert("SCOTUS DEALS BLOW TO TARIFF PLAN", "x")
    assert a["shouty"] is True
    b = ca.analyze_alert("Supreme Court deals blow to tariff plan", "x")
    assert b["shouty"] is False


def test_urgency_markers_found():
    a = ca.analyze_alert("BREAKING: developing story", "Live updates")
    assert "breaking" in a["urgency_markers"]
    assert a["urgency_count"] >= 2


def test_summarize_percentages_are_sane():
    rows = [
        {"outlet": "X", "title": "BREAKING: a thing happened",
         "body": "Details that add real information here.", "category": None},
        {"outlet": "X", "title": "Subscribe and save", "body": "Limited time offer.",
         "category": None},
    ]
    out = ca.summarize(rows)["outlets"][0]
    assert out["n"] == 2
    assert 0 <= out["promotional_pct"] <= 100
    assert out["restatement_pct"] + out["reason_to_open_pct"] == pytest.approx(100.0)


def test_summarize_handles_empty():
    assert ca.summarize([])["outlets"] == []


# --------------------------------------------------------------------------
# collector decoding
# --------------------------------------------------------------------------

def test_decode_single_object():
    assert collector._decode('{"a":1}') == [{"a": 1}]


def test_decode_pretty_printed_object_is_not_shredded():
    """Regression: newlines inside a pretty-printed object must not be read
    as an NDJSON delimiter."""
    body = '{\n  "event": "posted",\n  "package": "com.abc.abcnews"\n}'
    assert collector._decode(body) == [
        {"event": "posted", "package": "com.abc.abcnews"}
    ]


def test_decode_ndjson():
    out = collector._decode('{"a":1}\n{"a":2}\n')
    assert out == [{"a": 1}, {"a": 2}]


def test_decode_array():
    assert collector._decode('[{"a":1},{"a":2}]') == [{"a": 1}, {"a": 2}]


def test_decode_empty():
    assert collector._decode("   ") == []


def test_decode_garbage_raises():
    with pytest.raises(json.JSONDecodeError):
        collector._decode("not json at all")


def test_collector_store_skips_removals_and_system(dbfile, monkeypatch):
    monkeypatch.setenv("PUSHOBS_DB", dbfile)
    monkeypatch.setattr(db, "DEFAULT_DB", dbfile)
    assert collector.store({"event": "removed", "package": "com.abc.abcnews",
                            "title": "x", "post_time_ms": 1756825020000}) is False
    assert collector.store({"event": "posted", "package": "com.android.systemui",
                            "title": "x", "post_time_ms": 1756825020000}) is False
    assert collector.store({"event": "posted", "package": "com.abc.abcnews",
                            "title": "Real alert", "post_time_ms": 1756825020000}) is True


# --------------------------------------------------------------------------
# seed data
# --------------------------------------------------------------------------

def test_seed_marks_every_row_synthetic(dbfile):
    seed_demo.seed(days=3, db_path=dbfile)
    s = db.stats(dbfile)
    assert s["alerts"] > 0
    assert s["real"] == 0, "seeded rows must never count as real"
    assert s["synthetic"] == s["alerts"]


def test_purge_removes_only_synthetic(dbfile):
    with db.connect(dbfile) as c:
        db.insert_alert(c, package="com.abc.abcnews", outlet="ABC News",
                        title="Real one", body="b",
                        posted_at="2026-09-02T14:57:00+00:00")
    seed_demo.seed(days=2, db_path=dbfile)
    assert db.stats(dbfile)["real"] == 1
    seed_demo.purge(dbfile)
    s = db.stats(dbfile)
    assert s["synthetic"] == 0
    assert s["real"] == 1, "purge must not touch real captured data"


def test_seed_is_deterministic(tmp_path):
    a, b = str(tmp_path / "a.db"), str(tmp_path / "b.db")
    seed_demo.seed(days=3, db_path=a, seed_val=42)
    seed_demo.seed(days=3, db_path=b, seed_val=42)
    assert db.stats(a)["alerts"] == db.stats(b)["alerts"]
