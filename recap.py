#!/usr/bin/env python3
"""9pm nightly recap: the day's alerts -> MiniMax -> your phone.

Covers what the brief asks for:
  * the day's top clusters and who broke each first, and by how much
  * tone and framing differences on the same story across outlets
  * who over-pushed, who stayed quiet, who sent promo dressed as news
  * optionally, one outlet's position relative to the field -- first-mover
    rate, volume vs peers, copy quality -- when FOCUS_OUTLET is set

DELIVERY
--------
ntfy.sh topic (no account, no server) with email and stdout fallbacks. The
topic is configurable and is NOT committed: set PUSHOBS_NTFY_TOPIC in .env or
the environment. Pick something unguessable -- an ntfy topic is a public URL,
so anyone who knows the name can read your recaps.

DEGRADATION
-----------
Every external dependency here is optional and the job still produces
something useful without it:

  no MINIMAX_API_KEY    -> a deterministic statistical digest, clearly marked
                           as "no LLM", still delivered
  anthropic not installed -> same
  API error / refusal   -> same digest plus the error, still delivered
  ntfy unreachable      -> email if configured, else stdout

A nightly job that dies silently is worse than one that sends a plain digest,
so nothing in here raises past main().

Usage:
  python recap.py                  yesterday-to-now recap for today
  python recap.py --date 2026-09-08
  python recap.py --dry-run        build it, print it, do not deliver
  python recap.py --no-llm         skip the API entirely
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import smtplib
import sys
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import copy_analysis  # noqa: E402
import db  # noqa: E402

LOG = logging.getLogger("recap")

# MiniMax serves the Anthropic Messages API at its own base URL, so the
# anthropic SDK is used unchanged; only the endpoint, key and model differ.
# https://platform.minimax.io/docs/api-reference/text-anthropic-api
MINIMAX_BASE_URL = os.environ.get("MINIMAX_BASE_URL", "https://api.minimax.io/anthropic")
MODEL = os.environ.get("MINIMAX_MODEL", "MiniMax-M2.7")
FOCUS_OUTLET = os.environ.get("FOCUS_OUTLET", "").strip() or None


# ---------------------------------------------------------------------------
# .env loading (no dependency on python-dotenv)
# ---------------------------------------------------------------------------

def load_env(path: str | None = None) -> None:
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.isfile(path):
        return
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))
    except OSError:
        LOG.warning("could not read %s", path)


# ---------------------------------------------------------------------------
# Gather the day
# ---------------------------------------------------------------------------

def _parse(ts):
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def gather(date: str | None = None, db_path: str | None = None,
           include_synthetic: bool = False) -> dict:
    """Everything the recap needs for one day, computed locally."""
    if date:
        day_start = datetime.fromisoformat(date).replace(tzinfo=timezone.utc)
    else:
        now = datetime.now().astimezone()
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return gather_window(day_start, day_start + timedelta(days=1), db_path, include_synthetic)


def gather_window(start: datetime, end: datetime, db_path: str | None = None,
                  include_synthetic: bool = False) -> dict:
    """The recap's numbers over an arbitrary window. The dashboard's focus
    card calls this so it can never disagree with the nightly recap about
    how first-mover rate or volume are computed."""
    db.init_db(db_path)
    day_start, day_end = start, end

    sql = """SELECT id, outlet, title, body, posted_at, category, cluster_id, synthetic
             FROM alerts WHERE posted_at >= ? AND posted_at < ?"""
    params = [day_start.isoformat(), day_end.isoformat()]
    if not include_synthetic:
        sql += " AND synthetic = 0"
    sql += " ORDER BY posted_at"

    with db.connect(db_path) as conn:
        rows = [dict(r) for r in conn.execute(sql, params)]
        labels = {r["cluster_id"]: dict(r) for r in conn.execute("SELECT * FROM clusters")}

    per_outlet = Counter(r["outlet"] or "unknown" for r in rows)
    kinds: dict[str, Counter] = defaultdict(Counter)
    hours: Counter = Counter()
    for r in rows:
        k = copy_analysis.classify_kind(r["title"] or "", r["body"] or "", r["category"])
        kinds[r["outlet"] or "unknown"][k] += 1
        d = _parse(r["posted_at"])
        if d:
            hours[d.astimezone().hour] += 1

    # Clusters with 2+ outlets, with lag tables.
    by_cluster: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        if r["cluster_id"]:
            by_cluster[r["cluster_id"]].append(r)

    clusters = []
    firsts: Counter = Counter()
    participations: Counter = Counter()
    lags: dict[str, list[float]] = defaultdict(list)

    for cid, members in by_cluster.items():
        earliest: dict[str, dict] = {}
        for m in members:
            o = m["outlet"] or "unknown"
            if o not in earliest or (m["posted_at"] or "") < (earliest[o]["posted_at"] or ""):
                earliest[o] = m
        if len(earliest) < 2:
            continue
        t0 = min(_parse(m["posted_at"]) for m in earliest.values())
        entries = sorted(
            (
                {
                    "outlet": o,
                    "title": m["title"],
                    "body": (m["body"] or "")[:200],
                    "lag_min": round((_parse(m["posted_at"]) - t0).total_seconds() / 60, 1),
                    "time": _parse(m["posted_at"]).astimezone().strftime("%H:%M"),
                }
                for o, m in earliest.items()
            ),
            key=lambda e: e["lag_min"],
        )
        firsts[entries[0]["outlet"]] += 1
        for e in entries:
            participations[e["outlet"]] += 1
            lags[e["outlet"]].append(e["lag_min"])
        clusters.append(
            {
                "label": (labels.get(cid, {}).get("label") or entries[0]["title"]),
                "first": entries[0]["outlet"],
                "outlets": len(entries),
                "entries": entries,
            }
        )

    clusters.sort(key=lambda c: -c["outlets"])

    leaderboard = []
    for o, n in participations.items():
        L = sorted(lags[o])
        leaderboard.append(
            {
                "outlet": o,
                "stories": n,
                "firsts": firsts.get(o, 0),
                "first_rate": round(100 * firsts.get(o, 0) / n, 1),
                "median_lag": L[len(L) // 2] if L else 0.0,
            }
        )
    leaderboard.sort(key=lambda r: -r["first_rate"])

    vols = sorted(per_outlet.values())
    return {
        "date": day_start.strftime("%Y-%m-%d"),
        "window": [day_start.isoformat(), day_end.isoformat()],
        "total": len(rows),
        "outlets": len(per_outlet),
        "per_outlet": per_outlet.most_common(),
        "median_volume": vols[len(vols) // 2] if vols else 0,
        "kinds": {o: dict(c) for o, c in kinds.items()},
        "hours": dict(sorted(hours.items())),
        "clusters": clusters,
        "leaderboard": leaderboard,
        "copy": copy_analysis.summarize(rows)["outlets"] if rows else [],
        "focus": FOCUS_OUTLET,
        "synthetic_included": include_synthetic,
    }


FOCUS_DISABLED_MESSAGE = "Set FOCUS_OUTLET to compare one outlet against the field."


def focus_summary(d: dict, focus: str | None = None) -> dict:
    """The focus outlet's position, as the recap's focus section states it:
    first-mover rate, volume vs the cross-outlet median, breaking share, and
    reason-to-open rate. Each value is None when the data cannot support it.

    When no focus outlet is configured (FOCUS_OUTLET unset/empty), this
    returns a neutral, disabled result instead of guessing an outlet."""
    focus = focus or d.get("focus") or FOCUS_OUTLET
    if not focus:
        return {
            "focus": None,
            "enabled": False,
            "message": FOCUS_DISABLED_MESSAGE,
            "outlets_active": d.get("outlets", 0),
            "total_alerts": d.get("total", 0),
        }
    vol = dict(d.get("per_outlet") or {})
    n = vol.get(focus, 0)
    median = d.get("median_volume") or 0
    kinds = (d.get("kinds") or {}).get(focus) or {}
    breaking = kinds.get("breaking", 0)
    copy_row = next((r for r in d.get("copy") or [] if r["outlet"] == focus), None)
    race_row = next((r for r in d.get("leaderboard") or [] if r["outlet"] == focus), None)
    peers = [r for r in d.get("leaderboard") or [] if r["outlet"] != focus]
    peer_rates = sorted(r["first_rate"] for r in peers)
    return {
        "focus": focus,
        "enabled": True,
        "alerts": n,
        "median_volume": median,
        "volume_vs_median": (round(n / median, 2) if median else None),
        "breaking_share_pct": (round(100 * breaking / n, 1) if n else None),
        "reason_to_open_pct": copy_row["reason_to_open_pct"] if copy_row else None,
        "restatement_pct": copy_row["restatement_pct"] if copy_row else None,
        "first_rate": race_row["first_rate"] if race_row else None,
        "firsts": race_row["firsts"] if race_row else 0,
        "shared_stories": race_row["stories"] if race_row else 0,
        "median_lag_min": race_row["median_lag"] if race_row else None,
        "peer_median_first_rate": (peer_rates[len(peer_rates) // 2] if peer_rates else None),
        "outlets_active": d.get("outlets", 0),
        "total_alerts": d.get("total", 0),
    }


# ---------------------------------------------------------------------------
# Local digest (the no-LLM path, and the LLM's input)
# ---------------------------------------------------------------------------

def digest_text(d: dict) -> str:
    L = [f"PUSH OBSERVATORY — {d['date']}",
         f"{d['total']} alerts from {d['outlets']} outlets"]
    if d["synthetic_included"]:
        L.append("!! INCLUDES SYNTHETIC DEMO DATA — NOT REAL FINDINGS !!")
    if not d["total"]:
        L.append("\nNo alerts captured. Check that the emulator is running and "
                 "`capture.py` is alive (see the Coverage tab).")
        return "\n".join(L)

    L.append(f"\nVOLUME (median {d['median_volume']}/outlet)")
    for o, n in d["per_outlet"][:12]:
        k = d["kinds"].get(o, {})
        promo = k.get("promotional", 0)
        L.append(f"  {o:<24} {n:>3}" + (f"   ({promo} promotional)" if promo else ""))

    if d["leaderboard"]:
        L.append("\nFIRST-MOVER (stories covered by 2+ outlets)")
        for r in d["leaderboard"][:12]:
            L.append(f"  {r['outlet']:<24} {r['firsts']}/{r['stories']} first "
                     f"({r['first_rate']}%), median lag {r['median_lag']}m")

    if d["clusters"]:
        L.append("\nTOP STORIES")
        for c in d["clusters"][:8]:
            L.append(f"  • {c['label'][:90]}")
            L.append(f"    {c['outlets']} outlets, {c['first']} first")
            for e in c["entries"][:6]:
                tag = "first" if e["lag_min"] == 0 else f"+{e['lag_min']:.0f}m"
                L.append(f"      {tag:>7}  {e['outlet']:<22} {(e['title'] or '')[:70]}")

    if not d.get("focus"):
        L.append(f"\nFOCUS OUTLET\n  {FOCUS_DISABLED_MESSAGE}")
        return "\n".join(L)

    focus = next((r for r in d["copy"] if r["outlet"] == d["focus"]), None)
    if focus:
        L.append(f"\n{d['focus'].upper()}")
        L.append(f"  {focus['n']} alerts (median {d['median_volume']} across outlets)")
        L.append(f"  reason-to-open {focus['reason_to_open_pct']}%, "
                 f"restates headline {focus['restatement_pct']}%")
        fr = next((r for r in d["leaderboard"] if r["outlet"] == d["focus"]), None)
        if fr:
            L.append(f"  first on {fr['firsts']}/{fr['stories']} shared stories "
                     f"({fr['first_rate']}%), median lag {fr['median_lag']}m")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# Claude
# ---------------------------------------------------------------------------

SYSTEM = """You are the analyst for a push-notification observatory. Each night you \
receive structured data on every push alert that major news apps sent that day, \
captured from an instrumented Android device.

Write a recap for a product leader who works on audience messaging for a news app. They \
already know the news; they care about how the alerts were sent, not what happened.

Cover, in this order:
1. The day's biggest cross-outlet stories: who broke each first, and by how many minutes.
2. Where outlets framed the same story differently -- quote the actual copy side by side \
when the contrast is sharp.
3. Volume: who over-pushed, who stayed quiet, and who sent promotional messages dressed \
as news.
4. If a focus outlet is given in the data, a dedicated section on it: first-mover rate, \
volume relative to peers, and copy quality (does the alert give a reason to open, or \
restate the headline?). If no focus outlet is given, skip this section entirely.

Rules:
- Be specific and quantitative. Cite outlet names, minute counts, and real copy.
- Lead with what is genuinely notable. If the day was unremarkable, say so plainly \
rather than inflating it.
- Do not speculate about anything the data does not show. If the sample is thin, say \
the sample is thin and give the count.
- No preamble, no restating the instructions. Around 400-500 words. Plain text, no \
markdown headers heavier than a short line of caps."""


def call_model(d: dict, timeout: float = 300.0) -> tuple[str | None, str | None]:
    """Returns (recap_text, error). Never raises."""
    api_key = os.environ.get("MINIMAX_API_KEY")
    if not api_key:
        return None, "MINIMAX_API_KEY not set"
    try:
        import anthropic
    except ImportError:
        return None, "anthropic package not installed (pip install anthropic)"

    payload = {
        "date": d["date"],
        "total_alerts": d["total"],
        "outlets_active": d["outlets"],
        "median_alerts_per_outlet": d["median_volume"],
        "volume_by_outlet": d["per_outlet"],
        "kind_mix_by_outlet": d["kinds"],
        "alerts_by_hour_local": d["hours"],
        "first_mover_leaderboard": d["leaderboard"],
        "copy_stats_by_outlet": d["copy"],
        "focus_outlet": d["focus"],
        "top_clusters": d["clusters"][:12],
    }
    focus_line = (f"The focus outlet is {d['focus']}." if d.get("focus")
                  else "No focus outlet is configured; skip the focus section.")
    user = (
        f"Here is today's captured push data as JSON.\n\n"
        f"```json\n{json.dumps(payload, indent=2, ensure_ascii=False)}\n```\n\n"
        f"Write the nightly recap. {focus_line}"
    )
    if d["synthetic_included"]:
        user += ("\n\nIMPORTANT: this data includes SYNTHETIC demo rows. Open the recap "
                 "with a one-line warning that it is not based on real captured data.")

    client = anthropic.Anthropic(api_key=api_key, base_url=MINIMAX_BASE_URL, timeout=timeout)
    # M2.x models always reason before answering and cannot turn it off, so
    # the budget is generous: reasoning tokens count against max_tokens and a
    # tight cap would truncate the visible recap. No thinking/beta parameters
    # are passed; MiniMax ignores what it does not support but there is no
    # need to rely on that.
    try:
        with client.messages.stream(
            model=MODEL,
            max_tokens=8000,
            system=SYSTEM,
            messages=[{"role": "user", "content": user}],
        ) as stream:
            msg = stream.get_final_message()
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"

    if getattr(msg, "stop_reason", None) == "refusal":
        det = getattr(msg, "stop_details", None)
        return None, f"model declined the request ({getattr(det, 'category', 'unknown')})"

    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
    return (text or None), (None if text else "empty response")


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------

def deliver_ntfy(title: str, body: str) -> bool:
    topic = os.environ.get("PUSHOBS_NTFY_TOPIC")
    if not topic:
        LOG.info("PUSHOBS_NTFY_TOPIC not set; skipping ntfy")
        return False
    server = os.environ.get("PUSHOBS_NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    url = f"{server}/{topic}"
    req = urllib.request.Request(
        url,
        data=body.encode("utf-8"),
        headers={
            "Title": title.encode("ascii", "ignore").decode(),
            "Priority": "default",
            "Tags": "newspaper",
            "Content-Type": "text/plain; charset=utf-8",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            ok = 200 <= resp.status < 300
            LOG.info("ntfy -> %s (%s)", url, resp.status)
            return ok
    except (urllib.error.URLError, OSError) as e:
        LOG.warning("ntfy delivery failed: %s", e)
        return False


def deliver_email(subject: str, body: str) -> bool:
    to = os.environ.get("PUSHOBS_EMAIL_TO")
    host = os.environ.get("PUSHOBS_SMTP_HOST")
    if not (to and host):
        return False
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ.get("PUSHOBS_EMAIL_FROM", to)
    msg["To"] = to
    msg.set_content(body)
    try:
        port = int(os.environ.get("PUSHOBS_SMTP_PORT", "587"))
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls()
            user = os.environ.get("PUSHOBS_SMTP_USER")
            pw = os.environ.get("PUSHOBS_SMTP_PASS")
            if user and pw:
                s.login(user, pw)
            s.send_message(msg)
        LOG.info("emailed recap to %s", to)
        return True
    except Exception as e:
        LOG.warning("email delivery failed: %s", e)
        return False


def deliver(title: str, body: str) -> str:
    if deliver_ntfy(title, body):
        return "ntfy"
    if deliver_email(title, body):
        return "email"
    print("\n" + "=" * 68)
    print(title)
    print("=" * 68)
    print(body)
    print("=" * 68 + "\n")
    return "stdout"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def build(date=None, db_path=None, use_llm=True, include_synthetic=False) -> tuple[str, str, dict]:
    d = gather(date, db_path, include_synthetic)
    digest = digest_text(d)

    if not use_llm or d["total"] == 0:
        reason = "--no-llm" if not use_llm else "no alerts to analyse"
        return f"Push Observatory {d['date']}", f"{digest}\n\n[no LLM recap: {reason}]", d

    text, err = call_model(d)
    if text:
        body = f"{text}\n\n— — —\n{digest}"
    else:
        LOG.warning("Claude unavailable (%s); delivering statistical digest only", err)
        body = f"{digest}\n\n[no LLM recap: {err}]"
    return f"Push Observatory {d['date']}", body, d


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Nightly Push Observatory recap")
    ap.add_argument("--date", help="YYYY-MM-DD (default: today, local)")
    ap.add_argument("--db")
    ap.add_argument("--dry-run", action="store_true", help="print, do not deliver")
    ap.add_argument("--no-llm", action="store_true", help="skip the Claude call")
    ap.add_argument("--include-synthetic", action="store_true",
                    help="include synthetic demo rows (they are excluded by default)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
    )
    load_env()

    try:
        title, body, d = build(args.date, args.db, not args.no_llm, args.include_synthetic)
    except Exception:
        LOG.exception("recap generation failed")
        return 1

    if args.dry_run:
        print(f"--- {title} ---\n{body}")
        return 0

    via = deliver(title, body)
    LOG.info("recap for %s delivered via %s (%d alerts)", d["date"], via, d["total"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
