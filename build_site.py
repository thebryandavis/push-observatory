#!/usr/bin/env python3
"""Build a fully static export of the Push Observatory dashboard.

Runs the demo dataset builder into a throwaway SQLite database, then drives
every GET endpoint the dashboard's front end calls through FastAPI's
TestClient (no server process needed) and writes each response as a JSON
file under ``site/data/``. The dashboard's ``index.html`` is copied to
``site/index.html`` with a small shim (``window.STATIC_BASE = "./data"``)
so the exact same front-end code fetches those files instead of hitting a
live API. Write actions (adding/deleting an observation note) are disabled
in that mode -- see the "read-only demo" branch in index.html.

The file-name mapping between an API call (path + query string) and the
JSON file it becomes is `map_path()` below. index.html's `apiPath()`
implements the identical mapping in JavaScript -- the two MUST stay in sync;
tests/test_static_export.py checks representative cases against both.

Usage:
    python build_site.py                  # writes ./site
    python build_site.py --out /tmp/site
    python build_site.py --days 30 --max-clusters 150
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from urllib.parse import parse_qsl

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "demo"))


# ---------------------------------------------------------------------------
# Path mapping -- must match index.html's apiPath() exactly
# ---------------------------------------------------------------------------

def map_path(path: str) -> str:
    """'/api/timeline?days=7&include_synthetic=true' -> 'timeline__days-7_include_synthetic-true.json'
    '/api/cluster/c123' -> 'cluster/c123.json'
    '/api/meta' -> 'meta.json'
    """
    base, _, qs = path.partition("?")
    file = base[len("/api/"):] if base.startswith("/api/") else base.lstrip("/")
    if qs:
        pairs = sorted(parse_qsl(qs, keep_blank_values=True), key=lambda kv: kv[0])
        if pairs:
            file += "__" + "_".join(f"{k}-{v}" for k, v in pairs)
    return file + ".json"


# Windows the demo UI actually offers (see index.html's #days select and the
# "real data only" checkbox): 7 and 30 day windows, synthetic included or not.
WINDOWED_ENDPOINTS = ["timeline", "race", "volume", "copy", "focus"]
WINDOWS = [7, 30]
SYNTH_FLAGS = ["true", "false"]


def build_site(out_dir: str, days: int = 30, seed: int = 20260902,
               max_clusters: int = 150, quiet: bool = False) -> dict:
    log = (lambda *a: None) if quiet else print

    data_dir = os.path.join(out_dir, "data")
    os.makedirs(data_dir, exist_ok=True)
    os.makedirs(os.path.join(data_dir, "cluster"), exist_ok=True)

    tmp_dir = tempfile.mkdtemp(prefix="pushobs_static_")
    db_path = os.path.join(tmp_dir, "demo.db")

    os.environ["PUSHOBS_DB"] = db_path
    os.environ["PUSHOBS_OBS_DIR"] = os.path.join(tmp_dir, "observations")
    os.environ.pop("FOCUS_OUTLET", None)  # publisher-neutral demo: no focus outlet

    import build_dataset as demo_build  # noqa: E402  (demo/build_dataset.py)
    import db  # noqa: E402
    from fastapi.testclient import TestClient  # noqa: E402
    from dashboard.app import app  # noqa: E402

    # db.py reads PUSHOBS_DB into a module-level global at import time, so if
    # some earlier import in this process already pinned db.DEFAULT_DB to a
    # different path (e.g. a previous test module's fixture), setting the env
    # var alone would not be enough -- override the global directly too.
    db.DEFAULT_DB = db_path

    log(f"[1/4] building demo dataset ({days} days, seed {seed}) -> {db_path}")
    stats = demo_build.build(days=days, db_path=db_path, seed_val=seed)
    log(f"      {stats}")

    client = TestClient(app)
    written: list[str] = []

    def fetch_and_save(request_path: str, save_as: str | None = None) -> dict:
        r = client.get(request_path)
        r.raise_for_status()
        rel = save_as or map_path(request_path)
        full = os.path.join(data_dir, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            json.dump(r.json(), fh, ensure_ascii=False)
        written.append(rel)
        return r.json()

    log("[2/4] exporting endpoints the UI calls")
    fetch_and_save("/api/meta")
    fetch_and_save("/api/monitoring")
    fetch_and_save("/api/observations")

    race_by_window: dict[int, dict] = {}
    for days_w in WINDOWS:
        for synth in SYNTH_FLAGS:
            qs = f"days={days_w}&include_synthetic={synth}"
            for ep in WINDOWED_ENDPOINTS:
                data = fetch_and_save(f"/api/{ep}?{qs}")
                if ep == "race" and synth == "true":
                    race_by_window[days_w] = data

    # Nightly recap: curate a full "big news" day rather than whatever the
    # wall clock happens to land on, so the static demo always shows a rich
    # sample -- but store it at the bare "/api/recap" path, because that is
    # what the UI actually requests (no query string).
    big_days = stats.get("big_news_days") or []
    with db.connect(db_path) as conn:
        all_dates = [r["d"] for r in conn.execute(
            "SELECT DISTINCT substr(posted_at,1,10) d FROM alerts ORDER BY d")]
    # Prefer a big-news day that isn't the very last (likely partial) day.
    candidates = [d for d in big_days if d in all_dates and d != (all_dates[-1] if all_dates else None)]
    recap_date = candidates[-1] if candidates else (big_days[-1] if big_days else (all_dates[-1] if all_dates else None))
    log(f"[3/4] baking nightly recap for {recap_date}")
    recap_json = fetch_and_save(
        f"/api/recap?date={recap_date}&include_synthetic=true",
        save_as="recap.json",
    )
    log(f"      recap: {recap_json['total']} alerts, {recap_json['outlets']} outlets")

    # Cluster drill-downs, capped sensibly: every distinct cluster id from the
    # richest (30-day) race view.
    cluster_ids: list[str] = []
    seen_ids = set()
    for c in race_by_window.get(30, {}).get("clusters", []):
        cid = c["cluster_id"]
        if cid not in seen_ids:
            seen_ids.add(cid)
            cluster_ids.append(cid)
    cluster_ids = cluster_ids[:max_clusters]
    log(f"[4/4] exporting {len(cluster_ids)} cluster drill-downs (capped at {max_clusters})")
    for cid in cluster_ids:
        fetch_and_save(f"/api/cluster/{cid}")

    # Copy dashboard HTML with the static-mode shim injected.
    src_html = os.path.join(HERE, "dashboard", "static", "index.html")
    with open(src_html, encoding="utf-8") as fh:
        html = fh.read()
    shim = '<script>window.STATIC_BASE = "./data";</script>\n<script>\n'
    if "<script>\n" in html:
        html = html.replace("<script>\n", shim, 1)
    else:
        html = html.replace("<script>", shim.rstrip("\n") + "<script>", 1)
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(html)

    # vercel.json: keep the demo out of search results.
    vercel_json = {
        "headers": [
            {
                "source": "/(.*)",
                "headers": [
                    {"key": "X-Robots-Tag", "value": "noindex, nofollow, noarchive"}
                ],
            }
        ]
    }
    with open(os.path.join(out_dir, "vercel.json"), "w", encoding="utf-8") as fh:
        json.dump(vercel_json, fh, indent=2)
        fh.write("\n")

    shutil.rmtree(tmp_dir, ignore_errors=True)

    size = sum(os.path.getsize(os.path.join(dp, f))
               for dp, _, fs in os.walk(out_dir) for f in fs)
    return {
        "out_dir": out_dir,
        "cluster_files": len(cluster_ids),
        "total_json_files": len(written),
        "size_bytes": size,
        "recap_date": recap_date,
        "stats": stats,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Build the static Push Observatory site")
    ap.add_argument("--out", default=os.path.join(HERE, "site"))
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--seed", type=int, default=20260902)
    ap.add_argument("--max-clusters", type=int, default=150)
    ap.add_argument("-q", "--quiet", action="store_true")
    args = ap.parse_args(argv)

    if os.path.isdir(args.out):
        shutil.rmtree(args.out)

    res = build_site(args.out, args.days, args.seed, args.max_clusters, args.quiet)
    mb = res["size_bytes"] / (1024 * 1024)
    print(f"\nsite written to {res['out_dir']}")
    print(f"  {res['total_json_files']} JSON files ({res['cluster_files']} cluster drill-downs)")
    print(f"  recap baked for {res['recap_date']}")
    print(f"  total size: {mb:.2f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
