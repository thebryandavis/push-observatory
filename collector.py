#!/usr/bin/env python3
"""Host-side collector for the NotificationListenerService APK.

This is the other half of `com.example.pushlistener/`. The app POSTs one JSON
object per notification event to http://10.0.2.2:8787/ingest (10.0.2.2 is the
emulator's alias for the host loopback); this server writes them into the same
`alerts` table that capture.py fills, with the same dedupe rule, so the two
capture paths can run side by side during a changeover.

It binds 127.0.0.1 by default. The emulator can still reach it, because
10.0.2.2 is routed to the host's loopback interface -- so nothing on your
network is exposed.

Run:
    python collector.py                 # port 8787
    python collector.py --port 9000
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db  # noqa: E402
import packages  # noqa: E402

LOG = logging.getLogger("collector")
MAX_BODY = 256 * 1024


def store(evt: dict, keep_unmapped: bool = False) -> bool:
    pkg = evt.get("package")
    if not pkg or packages.is_system_package(pkg):
        return False

    # `removed` events are recorded for cancel-latency analysis but must not
    # create a new alert row -- the post event already did that.
    if evt.get("event") == "removed":
        LOG.debug("removal: %s %s", pkg, (evt.get("title") or "")[:50])
        return False

    outlet = packages.outlet_for(pkg)
    if outlet is None:
        if not keep_unmapped:
            LOG.info("unmapped package %s", pkg)
            return False
        outlet = f"UNMAPPED:{pkg}"

    post_ms = evt.get("post_time_ms")
    posted = (
        datetime.fromtimestamp(post_ms / 1000, tz=timezone.utc).isoformat(timespec="seconds")
        if isinstance(post_ms, (int, float)) and post_ms > 0
        else db.utcnow()
    )
    title = evt.get("title")
    body = evt.get("big_text") or evt.get("text")

    if not title and not body:
        return False

    with db.connect() as conn:
        return db.insert_alert(
            conn,
            package=pkg,
            outlet=outlet,
            title=title,
            body=body,
            posted_at=posted,
            category=evt.get("category") or evt.get("channel_id"),
            url=None,
            section="live",
            raw={**evt, "source": "listener"},
        )


def _decode(raw: str) -> list[dict]:
    """Accept a single JSON object/array, or newline-delimited JSON.

    A whole-body parse is attempted FIRST. Branching on "is there a newline"
    is wrong: a pretty-printed single object contains newlines and would be
    shredded into unparseable fragments. NDJSON is only tried once the body
    has failed to parse as one document.
    """
    raw = raw.strip()
    if not raw:
        return []
    try:
        doc = json.loads(raw)
        return doc if isinstance(doc, list) else [doc]
    except json.JSONDecodeError:
        pass

    events, errors = [], []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError as e:
            errors.append(e)
    if not events and errors:
        raise errors[0]
    return events


class Handler(BaseHTTPRequestHandler):
    keep_unmapped = False

    def _reply(self, code: int, payload: dict):
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.rstrip("/") in ("/healthz", ""):
            self._reply(200, {"ok": True, **db.stats()})
        else:
            self._reply(404, {"error": "not found"})

    def do_POST(self):
        if self.path.rstrip("/") != "/ingest":
            self._reply(404, {"error": "not found"})
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0 or n > MAX_BODY:
            self._reply(400, {"error": "bad content length"})
            return

        try:
            raw = self.rfile.read(n).decode("utf-8", errors="replace")
        except OSError:
            self._reply(400, {"error": "read failed"})
            return

        inserted = 0
        try:
            events = _decode(raw)
            for e in events:
                if isinstance(e, dict) and store(e, self.keep_unmapped):
                    inserted += 1
                    LOG.info("stored %s | %s", e.get("package"),
                             (e.get("title") or "")[:70])
        except json.JSONDecodeError as e:
            self._reply(400, {"error": f"bad json: {e}"})
            return
        except Exception:
            LOG.exception("ingest failed")
            # 500 makes the app spool and retry rather than lose the event.
            self._reply(500, {"error": "internal"})
            return

        self._reply(200, {"ok": True, "inserted": inserted})

    def log_message(self, fmt, *args):
        LOG.debug(fmt, *args)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Collector for the PushListener APK")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--host", default="127.0.0.1",
                    help="bind address (default loopback; the emulator reaches it "
                         "via 10.0.2.2)")
    ap.add_argument("--keep-unmapped", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
    )
    db.init_db()
    Handler.keep_unmapped = args.keep_unmapped

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    LOG.info("collector listening on http://%s:%d/ingest", args.host, args.port)
    LOG.info("emulator should POST to http://10.0.2.2:%d/ingest", args.port)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        LOG.info("stopping")
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
