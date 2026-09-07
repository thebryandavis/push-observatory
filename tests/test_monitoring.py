"""monitoring.py: pure functions over capture_log rows and canned adb output."""

from __future__ import annotations

import pathlib
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import db  # noqa: E402
import monitoring as mon  # noqa: E402

NOW = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)


def log(seconds_ago, ok=True, note="device:emulator-5554"):
    return {"ts": (NOW - timedelta(seconds=seconds_ago)).isoformat(), "ok": 1 if ok else 0,
            "parsed": 3, "inserted": 1, "note": note}


# --------------------------------------------------------------------------
# Liveness
# --------------------------------------------------------------------------

def test_liveness_never():
    r = mon.capture_liveness([], NOW)
    assert r["state"] == "never" and r["last_poll"] is None


def test_liveness_live_stale_dead():
    assert mon.capture_liveness([log(20)], NOW)["state"] == "live"
    assert mon.capture_liveness([log(400)], NOW)["state"] == "stale"
    assert mon.capture_liveness([log(3600)], NOW)["state"] == "dead"


def test_liveness_failing_counts_consecutive_failures():
    rows = [log(10, ok=False, note="no device/emulator connected"), log(40, ok=False), log(70, ok=True)]
    r = mon.capture_liveness(rows, NOW)
    assert r["state"] == "failing" and r["consecutive_failures"] == 2
    assert "no device" in r["explanation"]
    assert r["polls_last_hour"] == 3 and r["failures_last_hour"] == 2


# --------------------------------------------------------------------------
# adb state
# --------------------------------------------------------------------------

def cp(out=b"", err=b"", rc=0):
    return subprocess.CompletedProcess([], rc, out, err)


def test_adb_state_no_adb():
    r = mon.adb_state(None)
    assert r["state"] == "no-adb" and "HUMAN STEP 1" in r["explanation"]


def test_adb_state_down_up_offline():
    assert mon.adb_state("/x/adb", lambda c, t: cp(b"List of devices attached\n\n"))["state"] == "down"
    up = mon.adb_state("/x/adb", lambda c, t: cp(b"List of devices attached\nemulator-5554\tdevice\n"))
    assert up["state"] == "up" and up["devices"] == [{"serial": "emulator-5554", "state": "device"}]
    off = mon.adb_state("/x/adb", lambda c, t: cp(b"List of devices attached\nemulator-5554\toffline\n"))
    assert off["state"] == "offline" and "booting" in off["explanation"]


def test_adb_state_errors_never_raise():
    def boom(c, t):
        raise OSError("no such file")
    assert mon.adb_state("/x/adb", boom)["state"] == "adb-error"
    assert mon.adb_state("/x/adb", lambda c, t: cp(b"", b"daemon not running", 1))["state"] == "adb-error"


# --------------------------------------------------------------------------
# Silence vs no news
# --------------------------------------------------------------------------

def test_outlet_verdicts_distinguish_instrument_from_outlet():
    seen = {
        "ABC News": (NOW - timedelta(hours=1)).isoformat(),
        "CNN": (NOW - timedelta(hours=10)).isoformat(),
        "NPR": (NOW - timedelta(hours=30)).isoformat(),
    }
    live = mon.capture_liveness([log(20)], NOW)
    rows = {r["outlet"]: r for r in mon.outlet_status(seen, live, NOW)}
    assert rows["ABC News"]["verdict"] == "receiving"
    assert rows["CNN"]["verdict"] == "quiet" and "lull" in rows["CNN"]["why"]
    assert rows["NPR"]["verdict"] == "silent" and "suspect the app" in rows["NPR"]["why"]
    assert rows["Reuters"]["verdict"] == "never"
    assert rows["Axios"]["verdict"] == "n/a"
    assert rows["NPR"]["hours_since"] == 30.0

    dead = mon.capture_liveness([log(7200)], NOW)
    rows = {r["outlet"]: r for r in mon.outlet_status(seen, dead, NOW)}
    for name in ("ABC News", "CNN", "NPR"):
        assert rows[name]["verdict"] == "unknown", "a dead instrument must not blame the outlet"
        assert "ours" in rows[name]["why"]
    assert rows["Reuters"]["verdict"] == "never"


def test_outlet_status_sorted_worst_first():
    seen = {"ABC News": NOW.isoformat(), "NPR": (NOW - timedelta(hours=48)).isoformat()}
    rows = mon.outlet_status(seen, mon.capture_liveness([log(5)], NOW), NOW)
    assert rows[0]["outlet"] == "NPR" and rows[-1]["verdict"] == "n/a"


# --------------------------------------------------------------------------
# Miss rate + packages
# --------------------------------------------------------------------------

def test_miss_rate():
    assert mon.miss_rate({})["measurable"] is False
    r = mon.miss_rate({"live": 90, "historical": 10})
    assert r["measurable"] and r["rate_pct"] == 10.0 and r["historical_first"] == 10


def test_package_coverage_without_snapshot():
    r = mon.package_coverage(None)
    assert r["measurable"] is False and r["missing"] == []
    assert all(e["installed"] is None for e in r["expected"])
    assert "--snapshot" in r["explanation"]


def test_package_coverage_with_snapshot_and_alternate():
    installed = ["com.abc.abcnews", "com.treemolabs.apps.cbsnews", "com.spotify.music", "com.android.chrome"]
    r = mon.package_coverage(installed, "2026-09-06T12:00:00+00:00")
    by = {e["outlet"]: e for e in r["expected"]}
    assert by["ABC News"]["installed"] is True and by["ABC News"]["via_alternate"] is False
    assert by["CBS News"]["installed"] is True and by["CBS News"]["via_alternate"] is True
    assert by["CBS News"]["found_as"] == "com.treemolabs.apps.cbsnews"
    assert by["CNN"]["installed"] is False
    assert {e["outlet"] for e in r["missing"]} >= {"CNN", "NPR", "The New York Times"}
    assert r["unexpected_third_party"] == ["com.spotify.music"]


def test_parse_pm_list():
    assert mon.parse_pm_list("package:b\npackage:a\nnoise\n") == ["a", "b"]


def test_snapshot_round_trip(tmp_path):
    p = str(tmp_path / "m.db")
    assert mon.latest_snapshot(p) == (None, None)
    ts = mon.store_snapshot(["com.abc.abcnews"], p, note="device:emulator-5554")
    pkgs, got_ts = mon.latest_snapshot(p)
    assert pkgs == ["com.abc.abcnews"] and got_ts == ts


# --------------------------------------------------------------------------
# report() end to end on an empty and a populated DB
# --------------------------------------------------------------------------

def test_report_empty_db(tmp_path):
    p = str(tmp_path / "e.db")
    r = mon.report(p, adb_path=None, now=NOW)
    assert r["capture"]["state"] == "never"
    assert r["device"]["state"] == "no-adb"
    assert r["miss_rate"]["measurable"] is False
    assert r["packages"]["measurable"] is False
    assert all(o["verdict"] in ("never", "n/a") for o in r["outlets"])


def test_report_ignores_synthetic_rows_for_silence(tmp_path):
    p = str(tmp_path / "s.db")
    db.init_db(p)
    with db.connect(p) as c:
        db.insert_alert(c, package="com.abc.abcnews", outlet="ABC News", title="syn", body="",
                        posted_at=NOW.isoformat(), synthetic=True)
        db.insert_alert(c, package="com.cnn.mobile.android.phone", outlet="CNN", title="real", body="",
                        posted_at=(NOW - timedelta(hours=1)).isoformat(), section="historical")
        db.insert_alert(c, package="com.unknown.newsapp", outlet=None, title="who", body="",
                        posted_at=NOW.isoformat())
        db.log_capture(c, True, 3, 2, "device:emulator-5554")
    r = mon.report(p, adb_path=None, now=datetime.now(timezone.utc))
    by = {o["outlet"]: o for o in r["outlets"]}
    assert by["ABC News"]["verdict"] == "never", "synthetic rows must not count as seen"
    assert by["CNN"]["verdict"] == "receiving"
    assert r["miss_rate"]["measurable"] and r["miss_rate"]["historical_first"] == 1
    assert r["unmapped"][0]["package"] == "com.unknown.newsapp"
    assert r["capture"]["state"] == "live"


def test_migration_adds_new_tables_to_phase_one_db(tmp_path):
    """A DB created by phase one has no observations / package_snapshot tables."""
    import sqlite3
    p = str(tmp_path / "old.db")
    conn = sqlite3.connect(p)
    conn.executescript("""
      CREATE TABLE alerts (id INTEGER PRIMARY KEY, outlet TEXT, package TEXT NOT NULL, title TEXT,
        body TEXT, posted_at TIMESTAMP, captured_at TIMESTAMP, category TEXT, url TEXT,
        cluster_id TEXT, raw TEXT);
      CREATE TABLE clusters (cluster_id TEXT PRIMARY KEY, label TEXT);
      CREATE TABLE capture_log (id INTEGER PRIMARY KEY, ts TIMESTAMP, ok INTEGER, parsed INTEGER,
        inserted INTEGER, note TEXT);
      INSERT INTO alerts (outlet, package, title) VALUES ('CNN', 'com.cnn.mobile.android.phone', 'kept');
    """)
    conn.commit(); conn.close()
    db.init_db(p)
    with db.connect(p) as c:
        tables = {r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"observations", "package_snapshot"} <= tables
        assert c.execute("SELECT title FROM alerts").fetchone()["title"] == "kept"
        nid = db.add_observation(c, outlet="CNN", note="migrated fine")
        assert nid == 1
