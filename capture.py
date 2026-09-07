#!/usr/bin/env python3
"""Push Observatory capture loop.

Polls `adb shell dumpsys notification --noredact` on a cadence, parses out
every notification, and writes new ones to SQLite.

DESIGN RULE: this process must never exit on its own. The data cannot be
backfilled, so an unattended crash at 2am costs a night of the dataset. Every
failure mode -- adb missing, emulator gone, device offline, dumpsys garbage,
DB locked -- is logged and retried. The only clean exits are SIGINT/SIGTERM
and `--once`.

Modes
-----
  capture.py                     poll forever (default 30s)
  capture.py --once              one poll, print a summary, exit
  capture.py --replay FILE       parse a saved dump instead of calling adb
                                 (this is how you test with no emulator)
  capture.py --save-raw DIR      also write each raw dump to DIR, for
                                 validating the parser against real output
  capture.py --dry-run           parse and report, write nothing

Examples
--------
  python capture.py --replay tests/fixtures/dumpsys_notification_sample.txt --dry-run
  python capture.py --once
  python capture.py --interval 30 --save-raw raw/
"""

from __future__ import annotations

import argparse
import logging
import os
import pathlib
import random
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db  # noqa: E402
import packages  # noqa: E402
from parser import ParsedNotification, parse_dumpsys  # noqa: E402

LOG = logging.getLogger("capture")

DEFAULT_INTERVAL = 30
MAX_BACKOFF = 300  # never wait more than 5 min between retries

_STOP = False


def _handle_signal(signum, _frame):
    global _STOP
    _STOP = True
    LOG.info("signal %s received, finishing current cycle and stopping", signum)


# ---------------------------------------------------------------------------
# adb plumbing
# ---------------------------------------------------------------------------

class AdbError(RuntimeError):
    """Any failure to get a dump out of the device."""


def find_adb(explicit: str | None = None) -> str | None:
    """Locate adb: explicit path, then PATH, then the usual SDK locations."""
    if explicit:
        return explicit if os.path.isfile(explicit) else None
    found = shutil.which("adb")
    if found:
        return found
    candidates = [
        os.path.expanduser("~/Library/Android/sdk/platform-tools/adb"),
        os.path.expanduser("~/Android/Sdk/platform-tools/adb"),
        "/usr/local/bin/adb",
        "/opt/homebrew/bin/adb",
    ]
    sdk = os.environ.get("ANDROID_SDK_ROOT") or os.environ.get("ANDROID_HOME")
    if sdk:
        candidates.insert(0, os.path.join(sdk, "platform-tools", "adb"))
    for c in candidates:
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


def _run(cmd: list[str], timeout: int = 45) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, errors="replace"
    )


def list_devices(adb: str) -> list[str]:
    """Serial numbers of devices in 'device' state."""
    try:
        p = _run([adb, "devices"], timeout=20)
    except (subprocess.TimeoutExpired, OSError) as e:
        raise AdbError(f"`adb devices` failed: {e}") from e
    if p.returncode != 0:
        raise AdbError(f"`adb devices` exit {p.returncode}: {p.stderr.strip()[:200]}")
    out = []
    for line in p.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            out.append(parts[0])
    return out


def fetch_dump(adb: str, serial: str | None = None) -> str:
    """Return raw `dumpsys notification --noredact` text, or raise AdbError."""
    cmd = [adb]
    if serial:
        cmd += ["-s", serial]
    cmd += ["shell", "dumpsys", "notification", "--noredact"]
    try:
        p = _run(cmd, timeout=60)
    except subprocess.TimeoutExpired as e:
        raise AdbError("dumpsys timed out after 60s") from e
    except OSError as e:
        raise AdbError(f"could not execute adb: {e}") from e

    if p.returncode != 0:
        raise AdbError(f"dumpsys exit {p.returncode}: {(p.stderr or p.stdout).strip()[:300]}")

    text = p.stdout or ""
    low = text.lower()
    if "device not found" in low or "no devices/emulators found" in low:
        raise AdbError("device not found")
    if not text.strip():
        raise AdbError("dumpsys returned empty output")
    # An older platform that rejects --noredact prints usage text. Fall back so
    # we still capture *something*, but flag it loudly -- redacted titles are
    # useless and we want that visible, not silent.
    if "Notification" not in text and "usage" in low:
        raise AdbError("device rejected --noredact (unsupported platform version)")
    return text


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------

def _posted_iso(rec: ParsedNotification, captured_at: str) -> str:
    if rec.post_time_ms:
        return datetime.fromtimestamp(
            rec.post_time_ms / 1000, tz=timezone.utc
        ).isoformat(timespec="seconds")
    # No usable timestamp: fall back to capture time so the alert is not lost,
    # and record the substitution in `raw` so the analysis can discount it.
    return captured_at


def ingest(
    records: list[ParsedNotification],
    *,
    dry_run: bool = False,
    db_path: str | None = None,
    keep_unmapped: bool = False,
) -> dict[str, int]:
    captured_at = db.utcnow()
    counts = {"parsed": len(records), "inserted": 0, "skipped_system": 0,
              "unmapped": 0, "redacted": 0}

    rows = []
    for rec in records:
        if packages.is_system_package(rec.package):
            counts["skipped_system"] += 1
            continue
        outlet = packages.outlet_for(rec.package)
        if outlet is None:
            counts["unmapped"] += 1
            if not keep_unmapped:
                # Still worth knowing about: log it once per cycle so a wrong
                # package id in packages.py surfaces instead of vanishing.
                LOG.info("unmapped package %s (title=%r)", rec.package, (rec.title or "")[:60])
                continue
            outlet = f"UNMAPPED:{rec.package}"
        if rec.redacted:
            counts["redacted"] += 1
        raw = rec.to_raw()
        raw["posted_at_is_fallback"] = rec.post_time_ms is None
        rows.append((rec, outlet, raw))

    if dry_run:
        counts["inserted"] = len(rows)
        return counts

    db.init_db(db_path)
    with db.connect(db_path) as conn:
        for rec, outlet, raw in rows:
            try:
                if db.insert_alert(
                    conn,
                    package=rec.package,
                    outlet=outlet,
                    title=rec.title,
                    body=rec.body,
                    posted_at=_posted_iso(rec, captured_at),
                    captured_at=captured_at,
                    category=rec.category or rec.channel,
                    url=rec.url,
                    section=rec.section,
                    raw=raw,
                ):
                    counts["inserted"] += 1
            except Exception:
                LOG.exception("insert failed for %s", rec.package)
    return counts


def _save_raw(text: str, directory: str) -> None:
    try:
        d = pathlib.Path(directory)
        d.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        (d / f"dumpsys-{stamp}.txt").write_text(text, encoding="utf-8")
    except Exception:
        LOG.exception("could not save raw dump (continuing)")


# ---------------------------------------------------------------------------
# Cycle + loop
# ---------------------------------------------------------------------------

def one_cycle(args, adb: str | None) -> tuple[bool, dict[str, int], str]:
    """Run a single capture. Returns (ok, counts, note). Never raises."""
    try:
        if args.replay:
            text = pathlib.Path(args.replay).read_text(encoding="utf-8", errors="replace")
            note = f"replay:{args.replay}"
        else:
            if not adb:
                return False, {}, "adb not found"
            serial = args.serial
            if not serial:
                devices = list_devices(adb)
                if not devices:
                    return False, {}, "no device/emulator connected"
                serial = devices[0]
            text = fetch_dump(adb, serial)
            note = f"device:{serial}"
            if args.save_raw:
                _save_raw(text, args.save_raw)

        records = parse_dumpsys(text, include_historical=not args.live_only)
        counts = ingest(
            records,
            dry_run=args.dry_run,
            db_path=args.db,
            keep_unmapped=args.keep_unmapped,
        )
        if counts.get("redacted"):
            note += f" REDACTED={counts['redacted']}(--noredact not taking effect!)"
        return True, counts, note
    except AdbError as e:
        return False, {}, str(e)
    except Exception as e:  # never let anything kill the loop
        LOG.exception("unexpected error in capture cycle")
        return False, {}, f"unexpected: {type(e).__name__}: {e}"


def record_cycle(ok: bool, counts: dict, note: str, db_path: str | None, dry_run: bool):
    if dry_run:
        return
    try:
        db.init_db(db_path)
        with db.connect(db_path) as conn:
            db.log_capture(conn, ok, counts.get("parsed", 0), counts.get("inserted", 0), note)
    except Exception:
        LOG.exception("could not write capture_log (continuing)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Push Observatory capture loop")
    ap.add_argument("--once", action="store_true", help="single poll then exit")
    ap.add_argument("--replay", metavar="FILE", help="parse a saved dumpsys file instead of adb")
    ap.add_argument("--interval", type=int, default=DEFAULT_INTERVAL, help="seconds between polls")
    ap.add_argument("--adb", help="explicit path to the adb binary")
    ap.add_argument("--serial", help="device serial (default: first connected)")
    ap.add_argument("--db", help="sqlite path (default: ./alerts.db or $PUSHOBS_DB)")
    ap.add_argument("--save-raw", metavar="DIR", help="archive each raw dump to DIR")
    ap.add_argument("--dry-run", action="store_true", help="parse and report, write nothing")
    ap.add_argument("--live-only", action="store_true",
                    help="ignore the Historical Notifications section")
    ap.add_argument("--keep-unmapped", action="store_true",
                    help="store alerts from packages not in packages.py")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
    )

    adb = None
    if not args.replay:
        adb = find_adb(args.adb)
        if not adb:
            LOG.error(
                "adb not found. Install the Android SDK platform-tools, or pass --adb. "
                "See README > HUMAN STEPS."
            )
            if args.once:
                return 2
            LOG.error("continuing anyway; will retry in case the SDK appears later")
        else:
            LOG.info("using adb at %s", adb)

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    if args.once or args.replay:
        ok, counts, note = one_cycle(args, adb)
        record_cycle(ok, counts, note, args.db, args.dry_run)
        if ok:
            LOG.info(
                "ok  parsed=%d inserted=%d unmapped=%d system=%d  (%s)",
                counts.get("parsed", 0), counts.get("inserted", 0),
                counts.get("unmapped", 0), counts.get("skipped_system", 0), note,
            )
            return 0
        LOG.error("failed: %s", note)
        return 1

    LOG.info("capture loop starting (interval=%ss). Ctrl-C to stop.", args.interval)
    consecutive_failures = 0
    while not _STOP:
        started = time.monotonic()
        if adb is None:
            adb = find_adb(args.adb)
        ok, counts, note = one_cycle(args, adb)
        record_cycle(ok, counts, note, args.db, args.dry_run)

        if ok:
            consecutive_failures = 0
            if counts.get("inserted"):
                LOG.info(
                    "captured %d new (parsed=%d) %s",
                    counts["inserted"], counts.get("parsed", 0), note,
                )
            else:
                LOG.debug("no new alerts (parsed=%d) %s", counts.get("parsed", 0), note)
            delay = args.interval
        else:
            consecutive_failures += 1
            # Exponential backoff with jitter, capped. Log at WARNING for the
            # first few then drop to INFO so an overnight outage does not
            # produce a gigabyte of logs.
            delay = min(args.interval * (2 ** min(consecutive_failures - 1, 5)), MAX_BACKOFF)
            delay = delay * (0.8 + 0.4 * random.random())
            level = logging.WARNING if consecutive_failures <= 3 else logging.INFO
            LOG.log(
                level, "capture failed (%d in a row): %s -- retrying in %.0fs",
                consecutive_failures, note, delay,
            )

        # Sleep in small slices so a signal is honoured promptly.
        elapsed = time.monotonic() - started
        remaining = max(0.0, delay - elapsed)
        while remaining > 0 and not _STOP:
            time.sleep(min(1.0, remaining))
            remaining -= 1.0

    LOG.info("capture loop stopped cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
