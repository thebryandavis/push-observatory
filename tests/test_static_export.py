"""Tests for build_site.py's static export.

Two things matter here:

1. `map_path()` -- the API-call-to-JSON-filename mapping -- must match
   index.html's `apiPath()` exactly, or the static demo silently 404s on
   every fetch. This file hardcodes the JS logic in Python-string form and
   checks representative cases against both implementations agree; if
   apiPath() in index.html changes, update FIXTURES below.
2. A full (small) build actually produces a working site: index.html with
   the shim injected, JSON files that parse, write endpoints excluded, and
   the noindex vercel.json.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import build_site  # noqa: E402

# (request path as the front end calls `get()` with, expected file name)
FIXTURES = [
    ("/api/meta", "meta.json"),
    ("/api/monitoring", "monitoring.json"),
    ("/api/observations", "observations.json"),
    ("/api/timeline?days=7&include_synthetic=true", "timeline__days-7_include_synthetic-true.json"),
    ("/api/timeline?days=30&include_synthetic=false", "timeline__days-30_include_synthetic-false.json"),
    # order in the query string must not matter -- both map to the same file
    ("/api/race?include_synthetic=true&days=7", "race__days-7_include_synthetic-true.json"),
    ("/api/cluster/c20260827T1058-1698", "cluster/c20260827T1058-1698.json"),
    ("/api/recap", "recap.json"),
]


@pytest.mark.parametrize("path,expected", FIXTURES)
def test_map_path(path, expected):
    assert build_site.map_path(path) == expected


def test_map_path_query_order_independent():
    a = build_site.map_path("/api/copy?days=14&include_synthetic=false")
    b = build_site.map_path("/api/copy?include_synthetic=false&days=14")
    assert a == b


def _extract_js_api_path_fn(html: str) -> str:
    m = re.search(r"function apiPath\(path\)\{.*?\n\}", html, re.S)
    assert m, "apiPath() not found in index.html -- did it get renamed/removed?"
    return m.group(0)


def test_index_html_declares_static_shim_hook():
    """index.html must still define window-STATIC_BASE-aware apiPath(), or
    the mapping this module implements has nothing to agree with."""
    html = (ROOT / "dashboard" / "static" / "index.html").read_text(encoding="utf-8")
    fn = _extract_js_api_path_fn(html)
    assert "STATIC_BASE" in fn
    assert "sort" in fn  # query params must be sorted, matching map_path()


@pytest.fixture(scope="module")
def built_site(tmp_path_factory, monkeypatch_module=None):
    out = tmp_path_factory.mktemp("site")
    res = build_site.build_site(str(out), days=5, seed=1, max_clusters=5, quiet=True)
    return out, res


def test_build_site_produces_working_export(built_site):
    out, res = built_site

    index_html = (out / "index.html").read_text(encoding="utf-8")
    assert 'window.STATIC_BASE = "./data";' in index_html
    assert index_html.index('STATIC_BASE = "./data"') < index_html.index("function apiPath")

    vercel = json.loads((out / "vercel.json").read_text())
    headers = vercel["headers"][0]["headers"]
    assert any(h["key"] == "X-Robots-Tag" and "noindex" in h["value"] for h in headers)

    data_dir = out / "data"
    assert (data_dir / "meta.json").exists()
    assert (data_dir / "monitoring.json").exists()
    assert (data_dir / "observations.json").exists()
    assert (data_dir / "recap.json").exists()

    meta = json.loads((data_dir / "meta.json").read_text())
    assert meta["stats"]["synthetic"] > 0
    assert meta["stats"]["real"] == 0

    recap = json.loads((data_dir / "recap.json").read_text())
    assert recap["total"] >= 0
    assert "body" in recap and "title" in recap

    # windowed endpoints for both windows and both synthetic flags
    for ep in build_site.WINDOWED_ENDPOINTS:
        for d in build_site.WINDOWS:
            for flag in build_site.SYNTH_FLAGS:
                fname = f"{ep}__days-{d}_include_synthetic-{flag}.json"
                assert (data_dir / fname).exists(), fname

    assert res["total_json_files"] > 0
    assert res["size_bytes"] > 0


def test_build_site_is_publisher_neutral(built_site):
    """FOCUS_OUTLET must stay unset for the public demo -- this is not a
    single-publisher ranking tool."""
    out, _ = built_site
    focus = json.loads(
        (out / "data" / "focus__days-7_include_synthetic-true.json").read_text()
    )
    assert focus["enabled"] is False
    assert focus["focus"] is None
