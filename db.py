"""SQLite storage for the Push Observatory.

Schema follows the project brief, plus a few operational columns:

  * ``dedupe_hash``    -- UNIQUE, derived from (package, title, posted_at).
                          This is what makes the 30s polling loop idempotent.
  * ``section``        -- 'live' or 'historical' (which part of the dumpsys
                          dump the record came from).
  * ``synthetic``      -- 1 for seeded demo rows. The dashboard refuses to
                          present synthetic rows as findings.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

DEFAULT_DB = os.environ.get(
    "PUSHOBS_DB",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "alerts.db"),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
  id           INTEGER PRIMARY KEY,
  outlet       TEXT,
  package      TEXT NOT NULL,
  title        TEXT,
  body         TEXT,
  posted_at    TIMESTAMP,
  captured_at  TIMESTAMP,
  category     TEXT,
  url          TEXT,
  cluster_id   TEXT,
  raw          TEXT,
  section      TEXT DEFAULT 'live',
  synthetic    INTEGER DEFAULT 0,
  dedupe_hash  TEXT UNIQUE
);

CREATE INDEX IF NOT EXISTS idx_alerts_posted   ON alerts(posted_at);
CREATE INDEX IF NOT EXISTS idx_alerts_outlet   ON alerts(outlet);
CREATE INDEX IF NOT EXISTS idx_alerts_cluster  ON alerts(cluster_id);
CREATE INDEX IF NOT EXISTS idx_alerts_package  ON alerts(package);

CREATE TABLE IF NOT EXISTS clusters (
  cluster_id   TEXT PRIMARY KEY,
  label        TEXT,
  first_outlet TEXT,
  first_at     TIMESTAMP,
  size         INTEGER,
  computed_at  TIMESTAMP,
  method       TEXT
);

CREATE TABLE IF NOT EXISTS capture_log (
  id           INTEGER PRIMARY KEY,
  ts           TIMESTAMP,
  ok           INTEGER,
  parsed       INTEGER,
  inserted     INTEGER,
  note         TEXT
);

-- Qualitative log: dated notes per outlet, optionally pointing at a capture
-- in observations/<package>/<date>/ (ref = "<package>/<date>/<file>").
CREATE TABLE IF NOT EXISTS observations (
  id           INTEGER PRIMARY KEY,
  ts           TIMESTAMP,
  outlet       TEXT,
  package      TEXT,
  note         TEXT NOT NULL,
  ref          TEXT,
  author       TEXT
);
CREATE INDEX IF NOT EXISTS idx_obs_outlet ON observations(outlet);

-- `pm list packages -3` snapshots, so installed-vs-expected coverage can be
-- shown while the emulator is down.
CREATE TABLE IF NOT EXISTS package_snapshot (
  id           INTEGER PRIMARY KEY,
  ts           TIMESTAMP,
  packages     TEXT,
  note         TEXT
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def dedupe_hash(package: str, title: str | None, posted_at: str | None) -> str:
    """The brief's dedupe tuple (pkg, title, postTime), hashed."""
    raw = f"{package}\x00{title or ''}\x00{posted_at or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


@contextmanager
def connect(path: str | None = None) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(path or DEFAULT_DB, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(path: str | None = None) -> None:
    with connect(path) as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns that older DBs may lack. Cheap and idempotent."""
    have = {r["name"] for r in conn.execute("PRAGMA table_info(alerts)")}
    for col, ddl in (
        ("section", "ALTER TABLE alerts ADD COLUMN section TEXT DEFAULT 'live'"),
        ("synthetic", "ALTER TABLE alerts ADD COLUMN synthetic INTEGER DEFAULT 0"),
        ("dedupe_hash", "ALTER TABLE alerts ADD COLUMN dedupe_hash TEXT"),
    ):
        if col not in have:
            conn.execute(ddl)
    # Tables added in phase two. CREATE IF NOT EXISTS is idempotent, so a
    # phase-one database picks them up on its next open with no data loss.
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "observations" not in tables or "package_snapshot" not in tables:
        conn.executescript(SCHEMA)


def add_observation(conn: sqlite3.Connection, *, outlet: str | None, note: str,
                    package: str | None = None, ref: str | None = None,
                    author: str | None = None, ts: str | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO observations (ts, outlet, package, note, ref, author) VALUES (?,?,?,?,?,?)",
        (ts or utcnow(), outlet, package, note.strip(), ref, author),
    )
    return int(cur.lastrowid)


def insert_alert(
    conn: sqlite3.Connection,
    *,
    package: str,
    outlet: str | None,
    title: str | None,
    body: str | None,
    posted_at: str | None,
    captured_at: str | None = None,
    category: str | None = None,
    url: str | None = None,
    section: str = "live",
    synthetic: bool = False,
    raw: Any = None,
) -> bool:
    """Insert one alert. Returns True if it was new, False if deduped."""
    h = dedupe_hash(package, title, posted_at)
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO alerts
          (outlet, package, title, body, posted_at, captured_at,
           category, url, cluster_id, raw, section, synthetic, dedupe_hash)
        VALUES (?,?,?,?,?,?,?,?,NULL,?,?,?,?)
        """,
        (
            outlet,
            package,
            title,
            body,
            posted_at,
            captured_at or utcnow(),
            category,
            url,
            json.dumps(raw, ensure_ascii=False) if raw is not None else None,
            section,
            1 if synthetic else 0,
            h,
        ),
    )
    return cur.rowcount > 0


def log_capture(
    conn: sqlite3.Connection, ok: bool, parsed: int, inserted: int, note: str = ""
) -> None:
    conn.execute(
        "INSERT INTO capture_log (ts, ok, parsed, inserted, note) VALUES (?,?,?,?,?)",
        (utcnow(), 1 if ok else 0, parsed, inserted, note[:500]),
    )


def stats(path: str | None = None) -> dict[str, Any]:
    with connect(path) as conn:
        row = conn.execute(
            """SELECT COUNT(*) n,
                      SUM(synthetic) syn,
                      MIN(posted_at) first,
                      MAX(posted_at) last,
                      COUNT(DISTINCT outlet) outlets
               FROM alerts"""
        ).fetchone()
        return {
            "alerts": row["n"] or 0,
            "synthetic": row["syn"] or 0,
            "real": (row["n"] or 0) - (row["syn"] or 0),
            "first": row["first"],
            "last": row["last"],
            "outlets": row["outlets"] or 0,
        }


if __name__ == "__main__":
    init_db()
    print(f"initialised {DEFAULT_DB}")
    print(stats())
