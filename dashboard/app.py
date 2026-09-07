#!/usr/bin/env python3
"""FastAPI backend for the Push Observatory dashboard.

Run:
    uvicorn dashboard.app:app --reload --port 8000
or:
    python dashboard/app.py

Four analysis views, per the brief:
  /api/timeline   swimlane, one row per outlet, x = time of day
  /api/race       "who went first" + lag leaderboard
  /api/volume     volume and send-time histograms
  /api/copy       copy analysis
  /api/focus      the focus-outlet-vs-field card, via recap.py's own functions

Instrument views (phase two):
  /api/monitoring    capture liveness, adb/emulator state, per-outlet silence,
                     miss-rate proxy, installed-vs-expected packages
  /api/observations  qualitative log: captures from observations/ + notes

Every endpoint must work against an empty database. That is not a nicety: the
dashboard has to be openable on day one, before a single alert has landed, and
an empty state that 500s is worse than no dashboard.

SYNTHETIC DATA: /api/meta reports how many rows are synthetic. The front end
shows a permanent banner whenever that count is non-zero, and every response
that includes synthetic rows carries `"contains_synthetic": true`.
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from typing import Annotated

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import cluster as clustering  # noqa: E402
import copy_analysis  # noqa: E402
import db  # noqa: E402
import monitoring  # noqa: E402
import observe  # noqa: E402
import packages  # noqa: E402
import recap  # noqa: E402

app = FastAPI(title="Push Observatory", version="2.0")

STATIC = os.path.join(HERE, "static")
if os.path.isdir(STATIC):
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

# Captures written by observe.py. Overridable so tests can point at a temp dir.
OBS_DIR = os.environ.get("PUSHOBS_OBS_DIR", observe.DEFAULT_OUT)
os.makedirs(OBS_DIR, exist_ok=True)
app.mount("/observations", StaticFiles(directory=OBS_DIR), name="observations")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rows(sql: str, params=()) -> list[dict]:
    db.init_db()
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(sql, params)]


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _local(dt: datetime) -> datetime:
    """Render in the viewer's local time. Send-time analysis in UTC would be
    meaningless -- 'do they push overnight' is a question about the reader's
    clock, not Greenwich's."""
    return dt.astimezone()


def _window_clause(days: int | None) -> tuple[str, list]:
    if not days:
        return "", []
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    return " AND posted_at >= ?", [since]


# ---------------------------------------------------------------------------
# Meta
# ---------------------------------------------------------------------------

@app.get("/api/meta")
def meta():
    s = db.stats()
    outlets = _rows(
        """SELECT outlet, COUNT(*) n, SUM(synthetic) syn,
                  MIN(posted_at) first, MAX(posted_at) last
           FROM alerts WHERE outlet IS NOT NULL
           GROUP BY outlet ORDER BY n DESC"""
    )
    log = _rows("SELECT ts, ok, parsed, inserted, note FROM capture_log "
                "ORDER BY id DESC LIMIT 1")
    tracked = [
        {
            "name": o.name,
            "package": o.package,
            "confidence": o.confidence,
            "has_rss": bool(o.rss),
            "beat": o.beat,
            "note": o.note,
        }
        for o in packages.OUTLETS
    ]
    return {
        "stats": s,
        "outlets": outlets,
        "tracked": tracked,
        "last_capture": log[0] if log else None,
        "synthetic_present": (s["synthetic"] or 0) > 0,
        "real_present": (s["real"] or 0) > 0,
        "generated_at": db.utcnow(),
    }


# ---------------------------------------------------------------------------
# View 1: timeline / swimlane
# ---------------------------------------------------------------------------

@app.get("/api/timeline")
def timeline(days: Annotated[int, Query(ge=1, le=90)] = 7,
             include_synthetic: bool = True):
    where, params = _window_clause(days)
    if not include_synthetic:
        where += " AND synthetic = 0"
    rows = _rows(
        f"""SELECT id, outlet, title, body, posted_at, category, cluster_id, synthetic
            FROM alerts WHERE posted_at IS NOT NULL {where}
            ORDER BY posted_at""",
        params,
    )

    lanes: dict[str, list] = defaultdict(list)
    contains_syn = False
    for r in rows:
        dt = _parse(r["posted_at"])
        if not dt:
            continue
        loc = _local(dt)
        contains_syn = contains_syn or bool(r["synthetic"])
        lanes[r["outlet"] or "unknown"].append(
            {
                "id": r["id"],
                "date": loc.strftime("%Y-%m-%d"),
                "hour": loc.hour + loc.minute / 60.0,
                "time": loc.strftime("%H:%M"),
                "title": r["title"],
                "body": (r["body"] or "")[:240],
                "kind": copy_analysis.classify_kind(
                    r["title"] or "", r["body"] or "", r["category"]
                ),
                "cluster_id": r["cluster_id"],
                "synthetic": bool(r["synthetic"]),
            }
        )

    ordered = sorted(lanes.items(), key=lambda kv: -len(kv[1]))
    return {
        "lanes": [{"outlet": k, "alerts": v, "n": len(v)} for k, v in ordered],
        "days": sorted({a["date"] for v in lanes.values() for a in v}),
        "contains_synthetic": contains_syn,
        "total": len(rows),
    }


# ---------------------------------------------------------------------------
# View 2: who went first
# ---------------------------------------------------------------------------

@app.get("/api/race")
def race(days: Annotated[int, Query(ge=1, le=90)] = 7,
         min_outlets: Annotated[int, Query(ge=2)] = 2,
         include_synthetic: bool = True):
    where, params = _window_clause(days)
    if not include_synthetic:
        where += " AND synthetic = 0"
    rows = _rows(
        f"""SELECT id, outlet, title, body, posted_at, cluster_id, synthetic
            FROM alerts
            WHERE cluster_id IS NOT NULL AND posted_at IS NOT NULL {where}
            ORDER BY posted_at""",
        params,
    )
    if not rows:
        return {
            "clusters": [], "leaderboard": [], "contains_synthetic": False,
            "note": "No clusters yet. Run `python cluster.py` after capturing alerts.",
        }

    by_cluster: dict[str, list[dict]] = defaultdict(list)
    contains_syn = False
    for r in rows:
        contains_syn = contains_syn or bool(r["synthetic"])
        by_cluster[r["cluster_id"]].append(r)

    labels = {c["cluster_id"]: c for c in _rows("SELECT * FROM clusters")}

    out = []
    wins: Counter[str] = Counter()
    appearances: Counter[str] = Counter()
    lag_totals: dict[str, list[float]] = defaultdict(list)

    for cid, members in by_cluster.items():
        # One row per outlet, at that outlet's earliest push.
        firsts: dict[str, dict] = {}
        followups: Counter[str] = Counter()
        for m in members:
            o = m["outlet"] or "unknown"
            followups[o] += 1
            cur = firsts.get(o)
            if cur is None or (m["posted_at"] or "") < (cur["posted_at"] or ""):
                firsts[o] = m
        if len(firsts) < min_outlets:
            continue

        t0 = min(_parse(m["posted_at"]) for m in firsts.values())
        entries = []
        for o, m in firsts.items():
            dt = _parse(m["posted_at"])
            lag = round((dt - t0).total_seconds() / 60, 1)
            entries.append(
                {
                    "outlet": o,
                    "title": m["title"],
                    "posted_at": m["posted_at"],
                    "time": _local(dt).strftime("%a %H:%M"),
                    "lag_min": lag,
                    "followups": followups[o] - 1,
                    "synthetic": bool(m["synthetic"]),
                }
            )
            appearances[o] += 1
            lag_totals[o].append(lag)
        entries.sort(key=lambda e: e["lag_min"])
        wins[entries[0]["outlet"]] += 1

        meta_row = labels.get(cid, {})
        out.append(
            {
                "cluster_id": cid,
                "label": meta_row.get("label") or entries[0]["title"],
                "first_outlet": entries[0]["outlet"],
                "first_at": entries[0]["posted_at"],
                "when": entries[0]["time"],
                "outlets": len(entries),
                "spread_min": entries[-1]["lag_min"],
                "entries": entries,
            }
        )

    out.sort(key=lambda c: (c["first_at"] or ""), reverse=True)

    leaderboard = []
    for o, n in appearances.items():
        lags = sorted(lag_totals[o])
        leaderboard.append(
            {
                "outlet": o,
                "stories": n,
                "firsts": wins.get(o, 0),
                "first_rate": round(100 * wins.get(o, 0) / n, 1) if n else 0.0,
                "median_lag_min": lags[len(lags) // 2] if lags else 0.0,
                "mean_lag_min": round(sum(lags) / len(lags), 1) if lags else 0.0,
            }
        )
    leaderboard.sort(key=lambda r: (-r["first_rate"], r["median_lag_min"]))

    return {
        "clusters": out[:200],
        "leaderboard": leaderboard,
        "contains_synthetic": contains_syn,
        "cluster_count": len(out),
    }


# ---------------------------------------------------------------------------
# View 3: volume and cadence
# ---------------------------------------------------------------------------

@app.get("/api/volume")
def volume(days: Annotated[int, Query(ge=1, le=90)] = 14,
           include_synthetic: bool = True):
    where, params = _window_clause(days)
    if not include_synthetic:
        where += " AND synthetic = 0"
    rows = _rows(
        f"""SELECT outlet, posted_at, synthetic FROM alerts
            WHERE posted_at IS NOT NULL {where}""",
        params,
    )

    per_day: dict[str, Counter] = defaultdict(Counter)
    hours: dict[str, list[int]] = defaultdict(lambda: [0] * 24)
    all_hours = [0] * 24
    weekday = Counter()
    weekend = Counter()
    overnight = Counter()
    totals = Counter()
    contains_syn = False

    for r in rows:
        dt = _parse(r["posted_at"])
        if not dt:
            continue
        loc = _local(dt)
        o = r["outlet"] or "unknown"
        contains_syn = contains_syn or bool(r["synthetic"])
        per_day[o][loc.strftime("%Y-%m-%d")] += 1
        hours[o][loc.hour] += 1
        all_hours[loc.hour] += 1
        totals[o] += 1
        if loc.weekday() >= 5:
            weekend[o] += 1
        else:
            weekday[o] += 1
        if loc.hour < 6:
            overnight[o] += 1

    dates = sorted({d for c in per_day.values() for d in c})
    n_days = max(len(dates), 1)

    outlets = []
    for o, total in totals.most_common():
        wd_days = len({d for d in per_day[o] if datetime.strptime(d, "%Y-%m-%d").weekday() < 5}) or 1
        we_days = len({d for d in per_day[o] if datetime.strptime(d, "%Y-%m-%d").weekday() >= 5}) or 1
        outlets.append(
            {
                "outlet": o,
                "total": total,
                "per_day": round(total / n_days, 2),
                "hours": hours[o],
                "series": [per_day[o].get(d, 0) for d in dates],
                "overnight_pct": round(100 * overnight[o] / total, 1) if total else 0,
                "weekday_per_day": round(weekday[o] / wd_days, 2),
                "weekend_per_day": round(weekend[o] / we_days, 2),
            }
        )

    med = sorted(x["per_day"] for x in outlets)
    return {
        "dates": dates,
        "outlets": outlets,
        "all_hours": all_hours,
        "median_per_day": med[len(med) // 2] if med else 0,
        "contains_synthetic": contains_syn,
    }


# ---------------------------------------------------------------------------
# View 4: copy analysis
# ---------------------------------------------------------------------------

@app.get("/api/copy")
def copy_view(days: Annotated[int, Query(ge=1, le=90)] = 14,
              include_synthetic: bool = True):
    where, params = _window_clause(days)
    if not include_synthetic:
        where += " AND synthetic = 0"
    db.init_db()
    with db.connect() as conn:
        rows = conn.execute(
            f"""SELECT outlet, title, body, category, synthetic FROM alerts
                WHERE posted_at IS NOT NULL {where}""",
            params,
        ).fetchall()

    summary = copy_analysis.summarize(rows)
    contains_syn = any(r["synthetic"] for r in rows)

    examples = []
    for r in rows[:4000]:
        a = copy_analysis.analyze_alert(r["title"], r["body"], r["category"])
        examples.append(
            {
                "outlet": r["outlet"],
                "title": r["title"],
                "body": (r["body"] or "")[:160],
                "framing": a["framing"],
                "kind": a["kind"],
                "chars": a["title_chars"],
                "emoji": a["has_emoji"],
                "shouty": a["shouty"],
                "synthetic": bool(r["synthetic"]),
            }
        )

    return {**summary, "examples": examples[:400], "contains_synthetic": contains_syn}


# ---------------------------------------------------------------------------
# Focus outlet vs. field -- the recap's focus section, live
# ---------------------------------------------------------------------------

@app.get("/api/focus")
def focus(days: Annotated[int, Query(ge=1, le=90)] = 7,
          include_synthetic: bool = True):
    """Same numbers the 9pm recap prints in its focus section, over the
    dashboard's window. recap.gather_window and recap.focus_summary do the
    arithmetic; this endpoint only chooses the window."""
    end = datetime.now(timezone.utc) + timedelta(minutes=1)
    start = end - timedelta(days=days)
    d = recap.gather_window(start, end, include_synthetic=include_synthetic)
    summary = recap.focus_summary(d)
    syn = 0
    if include_synthetic:
        syn = _rows("SELECT COUNT(*) n FROM alerts WHERE synthetic = 1 AND posted_at >= ?",
                    (start.isoformat(),))[0]["n"]
    return {**summary, "days": days, "contains_synthetic": syn > 0,
            "note": ("Computed by recap.gather_window / recap.focus_summary -- the same "
                     "code path as the nightly recap's focus section.")}


# ---------------------------------------------------------------------------
# Monitoring -- is the instrument working?
# ---------------------------------------------------------------------------

@app.get("/api/monitoring")
def monitoring_view():
    return monitoring.report()


# ---------------------------------------------------------------------------
# Nightly recap -- the no-LLM statistical digest recap.py delivers each night
# ---------------------------------------------------------------------------

@app.get("/api/recap")
def recap_view(date: str | None = None, include_synthetic: bool | None = None):
    """The same digest text the nightly job emails/ntfy's, computed with
    ``--no-llm`` so this endpoint never needs an API key. Defaults to
    including synthetic rows only when there is no real data at all, so a
    freshly-seeded demo shows a recap and a real deployment does not
    accidentally recap invented data."""
    s = db.stats()
    if include_synthetic is None:
        include_synthetic = (s["real"] or 0) == 0
    if date is None:
        last = s.get("last")
        date = (last or db.utcnow())[:10]
    title, body, d = recap.build(
        date=date, use_llm=False, include_synthetic=include_synthetic
    )
    return {
        "title": title,
        "body": body,
        "date": d["date"],
        "total": d["total"],
        "outlets": d["outlets"],
        "contains_synthetic": bool(include_synthetic and d["total"]),
    }


# ---------------------------------------------------------------------------
# Observations -- the qualitative log
# ---------------------------------------------------------------------------

class NoteIn(BaseModel):
    outlet: str = Field(min_length=1, max_length=80)
    note: str = Field(min_length=1, max_length=8000)
    ref: str | None = Field(default=None, max_length=300)
    author: str | None = Field(default=None, max_length=80)


def _capture_sessions() -> dict[str, list[dict]]:
    """Walk OBS_DIR/<package>/<date>/steps.json -> {package: [session, ...]}."""
    out: dict[str, list[dict]] = defaultdict(list)
    if not os.path.isdir(OBS_DIR):
        return out
    for pkg in sorted(os.listdir(OBS_DIR)):
        pdir = os.path.join(OBS_DIR, pkg)
        if not os.path.isdir(pdir) or pkg.startswith("."):
            continue
        for date in sorted(os.listdir(pdir), reverse=True):
            ddir = os.path.join(pdir, date)
            mpath = os.path.join(ddir, "steps.json")
            if not os.path.isfile(mpath):
                continue
            try:
                with open(mpath, encoding="utf-8") as fh:
                    m = json.load(fh)
            except (OSError, ValueError):
                continue
            steps = []
            for st in m.get("steps", []):
                sm = st.get("summary") or {}
                steps.append({
                    "number": st.get("number"), "label": st.get("label"), "ts": st.get("ts"),
                    "ok": bool(st.get("ok")), "error": st.get("error"),
                    "png": f"/observations/{pkg}/{date}/{st['png']}" if st.get("png") else None,
                    "xml": f"/observations/{pkg}/{date}/{st['xml']}" if st.get("xml") else None,
                    "ref": f"{pkg}/{date}/{st.get('png') or ''}",
                    "toggles": sm.get("toggles", []),
                    "texts": sm.get("texts", [])[:40],
                    "buttons": [b["label"] for b in sm.get("buttons", [])],
                })
            out[pkg].append({
                "date": date, "sessions": m.get("sessions", []), "steps": steps,
                "channels": m.get("channels"),
                "index": f"/observations/{pkg}/{date}/index.md",
            })
    return out


@app.get("/api/observations")
def observations(outlet: str | None = None):
    notes = _rows("SELECT id, ts, outlet, package, note, ref, author FROM observations "
                  "ORDER BY ts DESC, id DESC")
    caps = _capture_sessions()
    by_outlet: dict[str, dict] = {}
    for o in packages.OUTLETS:
        by_outlet[o.name] = {"outlet": o.name, "package": o.package, "captures": [], "notes": []}
    for pkg, sessions in caps.items():
        name = packages.outlet_for(pkg) or pkg
        by_outlet.setdefault(name, {"outlet": name, "package": pkg, "captures": [], "notes": []})
        by_outlet[name]["captures"].extend(sessions)
    for n in notes:
        name = n["outlet"] or "(general)"
        by_outlet.setdefault(name, {"outlet": name, "package": n["package"], "captures": [], "notes": []})
        by_outlet[name]["notes"].append(n)
    rows = list(by_outlet.values())
    if outlet:
        rows = [r for r in rows if r["outlet"] == outlet]
    rows.sort(key=lambda r: (-(len(r["captures"]) + len(r["notes"])), r["outlet"]))
    return {
        "outlets": rows,
        "note_count": len(notes),
        "capture_count": sum(len(s) for s in caps.values()),
        "obs_dir": OBS_DIR,
        "protocol_steps": [{"label": k, "why": v} for k, v in observe.PROTOCOL_STEPS],
    }


@app.post("/api/observations", status_code=201)
def add_note(body: NoteIn):
    o = next((o for o in packages.OUTLETS if o.name == body.outlet), None)
    db.init_db()
    with db.connect() as conn:
        nid = db.add_observation(conn, outlet=body.outlet, note=body.note,
                                 package=o.package if o else None, ref=body.ref,
                                 author=body.author)
    return {"id": nid, "ok": True}


@app.delete("/api/observations/{note_id}")
def delete_note(note_id: int):
    db.init_db()
    with db.connect() as conn:
        cur = conn.execute("DELETE FROM observations WHERE id = ?", (note_id,))
    if cur.rowcount == 0:
        raise HTTPException(404, "no such note")
    return {"ok": True, "deleted": note_id}


# ---------------------------------------------------------------------------
# Detail + static
# ---------------------------------------------------------------------------

@app.get("/api/cluster/{cluster_id}")
def cluster_detail(cluster_id: str):
    rows = _rows(
        """SELECT id, outlet, title, body, posted_at, url, synthetic
           FROM alerts WHERE cluster_id = ? ORDER BY posted_at""",
        (cluster_id,),
    )
    return {"cluster_id": cluster_id, "alerts": rows}


@app.get("/healthz")
def healthz():
    try:
        db.init_db()
        return {"ok": True, **db.stats()}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.get("/")
def index():
    path = os.path.join(STATIC, "index.html")
    if not os.path.isfile(path):
        return JSONResponse({"error": "index.html missing"}, status_code=500)
    return FileResponse(path)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", 8000)))
