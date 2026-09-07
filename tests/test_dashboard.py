"""Dashboard endpoint tests.

Calls the endpoint functions directly rather than over HTTP, so no test client
or extra HTTP dependency is needed. The contract that matters is: every view
returns a well-formed payload against an EMPTY database and against seeded
data. An empty state that raises is worse than no dashboard, because the
dashboard has to be openable on day one before any alert has landed.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import cluster  # noqa: E402
import db  # noqa: E402
import seed_demo  # noqa: E402
from dashboard import app as dash  # noqa: E402


@pytest.fixture()
def empty_db(tmp_path, monkeypatch):
    p = str(tmp_path / "empty.db")
    monkeypatch.setattr(db, "DEFAULT_DB", p)
    db.init_db(p)
    return p


@pytest.fixture()
def seeded_db(tmp_path, monkeypatch):
    p = str(tmp_path / "seeded.db")
    monkeypatch.setattr(db, "DEFAULT_DB", p)
    db.init_db(p)
    seed_demo.seed(days=4, db_path=p)
    cluster.run(db_path=p)
    return p


# --------------------------------------------------------------------------
# Empty database
# --------------------------------------------------------------------------

def test_meta_on_empty_db(empty_db):
    m = dash.meta()
    assert m["stats"]["alerts"] == 0
    assert m["synthetic_present"] is False
    assert len(m["tracked"]) >= 18


def test_all_views_render_on_empty_db(empty_db):
    assert dash.timeline(days=7)["lanes"] == []
    assert dash.race(days=7)["clusters"] == []
    assert dash.volume(days=7)["outlets"] == []
    assert dash.copy_view(days=7)["outlets"] == []
    assert dash.volume(days=7)["all_hours"] == [0] * 24


def test_healthz_on_empty_db(empty_db):
    assert dash.healthz()["ok"] is True


# --------------------------------------------------------------------------
# Seeded database
# --------------------------------------------------------------------------

def test_meta_flags_synthetic(seeded_db):
    m = dash.meta()
    assert m["synthetic_present"] is True
    assert m["real_present"] is False
    assert m["stats"]["synthetic"] == m["stats"]["alerts"]


def test_timeline_shape(seeded_db):
    t = dash.timeline(days=7)
    assert t["lanes"] and t["total"] > 0
    assert t["contains_synthetic"] is True
    a = t["lanes"][0]["alerts"][0]
    assert 0 <= a["hour"] < 24
    assert a["kind"] in {"breaking", "promotional", "standard"}
    assert a["synthetic"] is True


def test_race_leaderboard_is_coherent(seeded_db):
    r = dash.race(days=7)
    assert r["clusters"], "seeded data must produce multi-outlet clusters"
    for row in r["leaderboard"]:
        assert 0 <= row["first_rate"] <= 100
        assert row["firsts"] <= row["stories"]
    for c in r["clusters"]:
        lags = [e["lag_min"] for e in c["entries"]]
        assert lags == sorted(lags), "entries must be ordered by lag"
        assert lags[0] == 0.0, "the first mover has zero lag by definition"
        assert len({e["outlet"] for e in c["entries"]}) == len(c["entries"]), \
            "an outlet must appear at most once per cluster"


def test_volume_shape(seeded_db):
    v = dash.volume(days=14)
    assert v["outlets"] and sum(v["all_hours"]) > 0
    for o in v["outlets"]:
        assert len(o["hours"]) == 24
        assert sum(o["hours"]) == o["total"]
        assert 0 <= o["overnight_pct"] <= 100


def test_copy_shape(seeded_db):
    c = dash.copy_view(days=14)
    assert c["outlets"]
    for o in c["outlets"]:
        assert o["restatement_pct"] + o["reason_to_open_pct"] == pytest.approx(100.0, abs=0.2)
        for k in ("promotional_pct", "breaking_pct", "emoji_pct", "shouty_pct"):
            assert 0 <= o[k] <= 100


def test_real_only_filter_excludes_synthetic(seeded_db):
    assert dash.timeline(days=7, include_synthetic=False)["total"] == 0
    assert dash.volume(days=7, include_synthetic=False)["outlets"] == []
    assert dash.copy_view(days=7, include_synthetic=False)["outlets"] == []


def test_cluster_detail(seeded_db):
    r = dash.race(days=7)
    cid = r["clusters"][0]["cluster_id"]
    d = dash.cluster_detail(cid)
    assert d["cluster_id"] == cid and len(d["alerts"]) >= 2


# --------------------------------------------------------------------------
# Phase two: focus card, monitoring, observations
# --------------------------------------------------------------------------

@pytest.fixture()
def obs_dir(tmp_path, monkeypatch):
    d = tmp_path / "obs"
    d.mkdir()
    monkeypatch.setattr(dash, "OBS_DIR", str(d))
    return d


@pytest.fixture()
def focus_outlet_env(monkeypatch):
    """Set FOCUS_OUTLET and keep recap.py's module-level default in sync,
    since recap reads the env var once at import time."""
    import recap
    monkeypatch.setenv("FOCUS_OUTLET", "ABC News")
    monkeypatch.setattr(recap, "FOCUS_OUTLET", "ABC News")
    return "ABC News"


def test_focus_on_empty_db(empty_db, focus_outlet_env):
    f = dash.focus(days=7)
    assert f["focus"] == focus_outlet_env and f["alerts"] == 0
    assert f["first_rate"] is None and f["volume_vs_median"] is None
    assert f["contains_synthetic"] is False


def test_focus_disabled_when_unset(empty_db, monkeypatch):
    """With no FOCUS_OUTLET configured, the card is disabled and neutral --
    it must not guess an outlet or fail."""
    import recap
    monkeypatch.delenv("FOCUS_OUTLET", raising=False)
    monkeypatch.setattr(recap, "FOCUS_OUTLET", None)
    f = dash.focus(days=7)
    assert f["focus"] is None
    assert f["enabled"] is False
    assert f["message"] == recap.FOCUS_DISABLED_MESSAGE


def test_focus_matches_recap_math(seeded_db, focus_outlet_env):
    import recap
    f = dash.focus(days=7, include_synthetic=True)
    assert f["alerts"] > 0 and f["contains_synthetic"] is True
    assert 0 <= f["first_rate"] <= 100
    assert f["reason_to_open_pct"] is not None
    # The card must be the recap's numbers, not a re-implementation: recompute
    # through recap on the same window and compare.
    from datetime import datetime, timedelta, timezone
    end = datetime.now(timezone.utc) + timedelta(minutes=1)
    d = recap.gather_window(end - timedelta(days=7), end, include_synthetic=True)
    s = recap.focus_summary(d)
    for k in ("alerts", "median_volume", "first_rate", "breaking_share_pct", "reason_to_open_pct"):
        assert f[k] == s[k], k
    # and excluding synthetic on an all-synthetic DB gives nothing
    assert dash.focus(days=7, include_synthetic=False)["alerts"] == 0


def test_monitoring_on_empty_db(empty_db):
    m = dash.monitoring_view()
    assert m["capture"]["state"] == "never"
    assert m["device"]["state"] in ("no-adb", "down", "up", "offline", "unauthorized", "adb-error")
    assert m["miss_rate"]["measurable"] is False
    assert m["packages"]["measurable"] is False
    assert len(m["outlets"]) >= 18
    assert all(o["verdict"] in ("never", "n/a") for o in m["outlets"])


def test_monitoring_treats_synthetic_as_unseen(seeded_db):
    m = dash.monitoring_view()
    assert all(o["verdict"] in ("never", "n/a") for o in m["outlets"]), \
        "synthetic demo rows must not make an outlet look alive"


def test_observations_empty(empty_db, obs_dir):
    o = dash.observations()
    assert o["note_count"] == 0 and o["capture_count"] == 0
    assert len(o["outlets"]) >= 18
    assert any(p["label"] == "alert-settings" for p in o["protocol_steps"])


def test_observations_notes_round_trip(empty_db, obs_dir):
    r = dash.add_note(dash.NoteIn(outlet="ABC News", note="Breaking toggle ON by default",
                                  ref="com.abc.abcnews/2026-09-05/07-alert-settings.png"))
    assert r["ok"] and r["id"] >= 1
    o = dash.observations(outlet="ABC News")
    assert len(o["outlets"]) == 1
    abc = o["outlets"][0]
    assert abc["package"] == "com.abc.abcnews"
    assert abc["notes"][0]["note"] == "Breaking toggle ON by default"
    assert abc["notes"][0]["ref"].endswith("07-alert-settings.png")
    assert dash.delete_note(r["id"])["deleted"] == r["id"]
    assert dash.observations(outlet="ABC News")["outlets"][0]["notes"] == []
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        dash.delete_note(r["id"])


def test_observations_renders_capture_sessions(empty_db, obs_dir):
    import observe
    from tests.test_observe import good_runner
    s = observe.Session("com.abc.abcnews", "first install", str(obs_dir), date="2026-09-05")
    s.capture(observe.Adb("/fake/adb", runner=good_runner()), "alert-settings")
    result, _ = observe.fetch_channels(observe.Adb("/fake/adb", runner=good_runner()), "com.abc.abcnews")
    s.record_channels(result)
    # a stray directory without a manifest must be ignored, not crash the view
    (obs_dir / "com.example.junk" / "2026-01-01").mkdir(parents=True)

    o = dash.observations()
    assert o["capture_count"] == 1
    abc = next(x for x in o["outlets"] if x["outlet"] == "ABC News")
    assert abc["captures"][0]["date"] == "2026-09-05"
    step = abc["captures"][0]["steps"][0]
    assert step["png"] == "/observations/com.abc.abcnews/2026-09-05/01-alert-settings.png"
    assert step["ok"] is True
    assert {t["label"]: t["checked"] for t in step["toggles"]}["Breaking News"] is True
    assert abc["captures"][0]["channels"]["channels"][0]["id"] == "breaking_news"
    assert o["outlets"][0]["outlet"] == "ABC News", "outlets with content sort first"


def test_note_validation():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        dash.NoteIn(outlet="", note="x")
    with pytest.raises(ValidationError):
        dash.NoteIn(outlet="ABC News", note="")
