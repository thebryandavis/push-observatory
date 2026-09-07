#!/usr/bin/env python3
"""Instrument health: is the observatory itself working?

The dashboard's analysis views answer "what did the outlets do". This module
answers the prior question: if an outlet looks silent, is that the outlet or
is it us? Every function here is pure over rows already in the database or
over injectable adb output, so all of it is testable with no device.

The one distinction that matters is spelled out in `outlet_status`:

    receiving   alerts in the last few hours
    quiet       nothing for a while, but the instrument is live -- a news lull
    silent      nothing for a long time while the instrument is live -- check
                the app (permission, in-app categories, package id)
    unknown     the capture loop is not live, so silence means nothing
    never       no alert has ever been captured from this outlet

`python monitoring.py --snapshot` stores `pm list packages -3` so the
installed-vs-expected diff survives the emulator being down.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db  # noqa: E402
import packages  # noqa: E402

DEFAULT_INTERVAL = 30           # capture.py's polling cadence, seconds
STALE_AFTER = 5 * DEFAULT_INTERVAL   # 2.5 min without a poll: stale
DEAD_AFTER = 20 * DEFAULT_INTERVAL   # 10 min: dead
QUIET_HOURS = 6                 # no alert for this long while live: quiet
SILENT_HOURS = 24               # no alert for this long while live: silent


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Capture-loop liveness
# ---------------------------------------------------------------------------

def capture_liveness(log_rows: list[dict], now: datetime | None = None,
                     interval: int = DEFAULT_INTERVAL) -> dict:
    """`log_rows` is capture_log newest-first (ts, ok, parsed, inserted, note)."""
    now = now or datetime.now(timezone.utc)
    if not log_rows:
        return {"state": "never", "last_poll": None, "seconds_since": None,
                "last_ok": None, "consecutive_failures": 0, "last_note": None,
                "polls_last_hour": 0, "failures_last_hour": 0,
                "explanation": "capture.py has never run against this database."}
    last = log_rows[0]
    last_dt = _parse(last["ts"])
    secs = (now - last_dt).total_seconds() if last_dt else None
    fails = 0
    for r in log_rows:
        if r["ok"]:
            break
        fails += 1
    hour_ago = now - timedelta(hours=1)
    recent = [r for r in log_rows if (_parse(r["ts"]) or hour_ago) >= hour_ago]
    if secs is None:
        state = "unknown"
    elif secs > DEAD_AFTER * max(1, interval // DEFAULT_INTERVAL):
        state = "dead"
    elif secs > STALE_AFTER * max(1, interval // DEFAULT_INTERVAL):
        state = "stale"
    elif not last["ok"]:
        state = "failing"
    else:
        state = "live"
    explanation = {
        "live": "polling on cadence and the last cycle succeeded.",
        "failing": f"polling, but the last {fails} cycle(s) failed: {last.get('note') or ''}",
        "stale": "no poll for a few minutes; the loop may be wedged or the machine asleep.",
        "dead": "no poll for over ten minutes. Silence from every outlet is meaningless until this is fixed.",
        "unknown": "the last capture_log timestamp could not be parsed.",
    }[state]
    return {
        "state": state,
        "last_poll": last["ts"],
        "seconds_since": round(secs) if secs is not None else None,
        "last_ok": bool(last["ok"]),
        "consecutive_failures": fails,
        "last_note": last.get("note"),
        "polls_last_hour": len(recent),
        "failures_last_hour": sum(1 for r in recent if not r["ok"]),
        "explanation": explanation,
    }


# ---------------------------------------------------------------------------
# adb / emulator state
# ---------------------------------------------------------------------------

def adb_state(adb_path: str | None, runner=None) -> dict:
    """Never raises. `runner(cmd, timeout)` is injectable for tests."""
    if not adb_path:
        return {"adb": None, "state": "no-adb", "devices": [],
                "explanation": "adb is not installed or not on PATH (README, HUMAN STEP 1)."}
    import subprocess
    run = runner or (lambda cmd, t: subprocess.run(cmd, capture_output=True, timeout=t))
    try:
        p = run([adb_path, "devices"], 15)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"adb": adb_path, "state": "adb-error", "devices": [],
                "explanation": f"`adb devices` failed: {e}"}
    if p.returncode != 0:
        return {"adb": adb_path, "state": "adb-error", "devices": [],
                "explanation": p.stderr.decode(errors="replace").strip()[:200] or "adb exited non-zero"}
    devs = []
    for line in p.stdout.decode(errors="replace").splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2:
            devs.append({"serial": parts[0], "state": parts[1]})
    if not devs:
        state, why = "down", "adb is installed but no emulator or device is connected."
    elif any(d["state"] == "device" for d in devs):
        state, why = "up", "a device is connected and ready."
    else:
        state = devs[0]["state"]
        why = {"offline": "the device is attached but offline -- usually still booting.",
               "unauthorized": "accept the USB-debugging prompt on the device."}.get(
            state, f"device state is '{state}'.")
    return {"adb": adb_path, "state": state, "devices": devs, "explanation": why}


# ---------------------------------------------------------------------------
# Per-outlet silence
# ---------------------------------------------------------------------------

def outlet_status(last_seen_by_outlet: dict[str, str | None], liveness: dict,
                  now: datetime | None = None) -> list[dict]:
    """One row per tracked outlet. `last_seen_by_outlet` maps outlet name to
    its most recent REAL posted_at (synthetic rows must be excluded upstream)."""
    now = now or datetime.now(timezone.utc)
    instrument_live = liveness.get("state") in ("live", "failing", "stale")
    out = []
    for o in packages.OUTLETS:
        last = last_seen_by_outlet.get(o.name)
        dt = _parse(last)
        hours = round((now - dt).total_seconds() / 3600, 1) if dt else None
        if not o.package:
            verdict, why = "n/a", "no Android app; absence is the finding."
        elif dt is None:
            verdict = "never"
            why = ("no alert ever captured. Not installed, permission off, in-app categories "
                   "off, or wrong package id -- check in that order.")
        elif not instrument_live:
            verdict = "unknown"
            why = f"last alert {hours}h ago, but the capture loop is {liveness.get('state')}: silence here is ours, not theirs."
        elif hours < QUIET_HOURS:
            verdict, why = "receiving", "alerts arriving."
        elif hours < SILENT_HOURS:
            verdict, why = "quiet", f"{hours}h without an alert while the instrument is live: probably a news lull."
        else:
            verdict = "silent"
            why = (f"{hours}h without an alert while the instrument is live. This is long enough to "
                   f"suspect the app: OS permission, in-app categories, or a session that logged out.")
        out.append({"outlet": o.name, "package": o.package, "confidence": o.confidence,
                    "last_seen": last, "hours_since": hours, "verdict": verdict, "why": why})
    order = {"silent": 0, "unknown": 1, "never": 2, "quiet": 3, "receiving": 4, "n/a": 5}
    out.sort(key=lambda r: (order[r["verdict"]], r["outlet"]))
    return out


# ---------------------------------------------------------------------------
# Parser miss-rate (proxy)
# ---------------------------------------------------------------------------

def miss_rate(section_counts: dict[str, int]) -> dict:
    """The polling loop reads both the live shade and the 'historical' section
    of the dump. Dedupe keeps an alert's FIRST sighting. An alert first seen
    in `historical` was posted and dismissed between two polls without ever
    being seen live, so historical-first / total is a floor on how often the
    30s cadence misses an alert's live window. It is a proxy, and it says
    nothing about alerts that vanished from history too."""
    total = sum(section_counts.values())
    hist = section_counts.get("historical", 0)
    if total == 0:
        return {"measurable": False, "rate_pct": None, "historical_first": 0, "total": 0,
                "explanation": "no real alerts captured yet, so nothing to measure."}
    return {"measurable": True, "rate_pct": round(100 * hist / total, 1),
            "historical_first": hist, "total": total,
            "explanation": "share of real alerts whose first sighting was in the dump's historical "
                           "section, i.e. missed live. A floor, not the full miss rate."}


# ---------------------------------------------------------------------------
# Installed-vs-expected packages
# ---------------------------------------------------------------------------

def package_coverage(installed: list[str] | None, snapshot_ts: str | None = None) -> dict:
    expected = []
    for o in packages.OUTLETS:
        if not o.package:
            continue
        found = None
        if installed is not None:
            for cand in (o.package, *o.alternates):
                if cand in installed:
                    found = cand
                    break
        expected.append({"outlet": o.name, "package": o.package, "confidence": o.confidence,
                         "installed": (found is not None) if installed is not None else None,
                         "found_as": found,
                         "via_alternate": bool(found and found != o.package)})
    known = {p for o in packages.OUTLETS for p in ((o.package,) + o.alternates) if p}
    unexpected = sorted(p for p in (installed or []) if p not in known
                        and not packages.is_system_package(p))
    return {
        "measurable": installed is not None,
        "snapshot_ts": snapshot_ts,
        "expected": expected,
        "installed_count": len(installed) if installed is not None else None,
        "missing": [e for e in expected if e["installed"] is False],
        "unexpected_third_party": unexpected,
        "explanation": ("diff of `pm list packages -3` against packages.py."
                        if installed is not None else
                        "no package snapshot stored yet. Run `python monitoring.py --snapshot` "
                        "with the emulator up."),
    }


def parse_pm_list(text: str) -> list[str]:
    return sorted(line.split(":", 1)[1].strip() for line in text.splitlines()
                  if line.startswith("package:"))


def store_snapshot(installed: list[str], db_path: str | None = None, note: str = "") -> str:
    db.init_db(db_path)
    ts = db.utcnow()
    with db.connect(db_path) as conn:
        conn.execute("INSERT INTO package_snapshot (ts, packages, note) VALUES (?,?,?)",
                     (ts, json.dumps(installed), note[:200]))
    return ts


def latest_snapshot(db_path: str | None = None) -> tuple[list[str] | None, str | None]:
    db.init_db(db_path)
    with db.connect(db_path) as conn:
        row = conn.execute("SELECT ts, packages FROM package_snapshot ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return None, None
    try:
        return json.loads(row["packages"]), row["ts"]
    except ValueError:
        return None, row["ts"]


# ---------------------------------------------------------------------------
# Everything at once (what /api/monitoring returns)
# ---------------------------------------------------------------------------

def report(db_path: str | None = None, adb_path: str | None = "auto", runner=None,
           now: datetime | None = None) -> dict:
    db.init_db(db_path)
    with db.connect(db_path) as conn:
        log = [dict(r) for r in conn.execute(
            "SELECT ts, ok, parsed, inserted, note FROM capture_log ORDER BY id DESC LIMIT 200")]
        last_seen = {r["outlet"]: r["last"] for r in conn.execute(
            "SELECT outlet, MAX(posted_at) last FROM alerts WHERE synthetic = 0 "
            "AND outlet IS NOT NULL GROUP BY outlet")}
        sections = {r["section"] or "live": r["n"] for r in conn.execute(
            "SELECT section, COUNT(*) n FROM alerts WHERE synthetic = 0 GROUP BY section")}
        unmapped = [dict(r) for r in conn.execute(
            "SELECT package, COUNT(*) n FROM alerts WHERE outlet IS NULL AND synthetic = 0 "
            "GROUP BY package ORDER BY n DESC LIMIT 20")]
    if adb_path == "auto":
        import observe
        adb_path = observe.find_adb()
    live = capture_liveness(log, now)
    installed, snap_ts = latest_snapshot(db_path)
    return {
        "generated_at": db.utcnow(),
        "capture": live,
        "device": adb_state(adb_path, runner),
        "outlets": outlet_status(last_seen, live, now),
        "miss_rate": miss_rate(sections),
        "packages": package_coverage(installed, snap_ts),
        "unmapped": unmapped,
        "recent_log": log[:20],
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Push Observatory instrument health")
    ap.add_argument("--snapshot", action="store_true",
                    help="store `pm list packages -3` from the connected device")
    ap.add_argument("--db")
    ap.add_argument("--adb")
    ap.add_argument("--serial")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    import observe
    if args.snapshot:
        adb = observe.Adb(observe.find_adb(args.adb), args.serial)
        try:
            serial = adb.require_device()
            installed = observe.installed_packages(adb)
        except observe.AdbError as e:
            print(f"monitoring.py: {e}", file=sys.stderr)
            return 1
        ts = store_snapshot(installed, args.db, note=f"device:{serial}")
        cov = package_coverage(installed, ts)
        print(f"stored {len(installed)} third-party packages at {ts}")
        for e in cov["missing"]:
            print(f"  MISSING {e['outlet']:<24} {e['package']}")
        for e in cov["expected"]:
            if e["via_alternate"]:
                print(f"  ALTERNATE {e['outlet']:<22} found as {e['found_as']} -- fix packages.py")
        return 0

    r = report(args.db, observe.find_adb(args.adb))
    if args.json:
        print(json.dumps(r, indent=2))
        return 0
    print(f"capture loop : {r['capture']['state']} — {r['capture']['explanation']}")
    print(f"device       : {r['device']['state']} — {r['device']['explanation']}")
    mr = r["miss_rate"]
    print(f"miss-rate    : {mr['rate_pct']}% ({mr['historical_first']}/{mr['total']})"
          if mr["measurable"] else f"miss-rate    : not measurable — {mr['explanation']}")
    pk = r["packages"]
    print(f"packages     : " + (f"{len(pk['missing'])} missing of {len(pk['expected'])} (snapshot {pk['snapshot_ts']})"
                                if pk["measurable"] else pk["explanation"]))
    print("outlets:")
    for o in r["outlets"]:
        print(f"  {o['verdict']:<9} {o['outlet']:<24} {o['hours_since'] if o['hours_since'] is not None else '—':>6}h  {o['why']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
