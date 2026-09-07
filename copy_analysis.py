"""Analysis of push copy: length, framing, urgency, emoji, breaking vs promotional.

The interesting question from the brief is not "how long is the copy" but
**does the alert give a reason to open, or just restate the headline?**

HOW `reason_to_open` IS SCORED, AND ITS LIMITS
----------------------------------------------
An alert has two fields: title and body. Three shapes occur in practice:

  restatement   body is absent, or is a near-duplicate of the title, or is
                pure boilerplate ("Read more at nytimes.com", "Tap to open").
                The user learns nothing by opening that they did not already
                know from the lock screen.
  adds_detail   body carries facts the title does not -- a number, a name, a
                consequence. The classic wire-service shape.
  hook          body poses a question, teases withheld information, or makes
                a second-person appeal ("here's what it means for you").

This is a heuristic, not a judgement of quality, and it is reported as a
distribution rather than a score per outlet. Boilerplate detection is the
part most likely to need tuning once real copy arrives -- BOILERPLATE below
is the list to extend.
"""

from __future__ import annotations

import re
from collections import Counter

# Emoji ranges (broad; covers pictographs, symbols, flags, dingbats).
_EMOJI = re.compile(
    "[" "\U0001F300-\U0001FAFF" "\U00002600-\U000027BF" "\U0001F1E6-\U0001F1FF"
    "\U00002190-\U00002BFF" "\U0000FE0F" "\U00002B00-\U00002BFF" "]"
)

URGENCY_MARKERS = (
    "breaking", "just in", "urgent", "developing", "live", "alert", "now",
    "update", "confirmed", "exclusive", "first", "watch live", "happening now",
)

PROMO_MARKERS = (
    "subscribe", "subscription", "sale", "% off", "offer", "trial", "deal",
    "free month", "limited time", "last chance", "renew", "upgrade", "save",
    "don't miss out", "sign up", "for $1", "membership",
)

# Second-person / curiosity constructions that promise a payoff for opening.
HOOK_MARKERS = (
    "here's what", "here is what", "what to know", "what it means", "why",
    "how to", "what's next", "what happens", "your", "you", "we asked",
    "the reason", "explains", "explainer", "take a look", "see the",
)

BOILERPLATE = (
    "read more", "tap to open", "tap for more", "open the app", "learn more",
    "full story", "read the full", "see more", "more details", "click here",
    "view in app", "read now", "get the latest",
)


def has_emoji(text: str) -> bool:
    return bool(_EMOJI.search(text or ""))


def emoji_list(text: str) -> list[str]:
    return _EMOJI.findall(text or "")


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower())


def _tokens(s: str) -> set[str]:
    return {t for t in _norm(s).split() if len(t) > 2}


def is_boilerplate(body: str) -> bool:
    low = (body or "").strip().lower()
    if not low:
        return True
    if len(low) < 25 and any(b in low for b in BOILERPLATE):
        return True
    # A body that is *only* a domain or a call to action.
    stripped = re.sub(r"https?://\S+", "", low).strip(" .-—…")
    if len(stripped) < 12:
        return True
    return any(low.startswith(b) for b in BOILERPLATE)


def restatement_overlap(title: str, body: str) -> float:
    """Fraction of body tokens already present in the title."""
    tb, bb = _tokens(title), _tokens(body)
    if not bb:
        return 1.0
    return len(tb & bb) / len(bb)


def classify_framing(title: str, body: str) -> str:
    """-> 'restatement' | 'adds_detail' | 'hook'"""
    body = (body or "").strip()
    title = (title or "").strip()

    low = f"{title} {body}".lower()
    if any(h in low for h in HOOK_MARKERS) and len(body) > 20:
        # A hook needs to actually promise something beyond the headline.
        if restatement_overlap(title, body) < 0.8:
            return "hook"

    if is_boilerplate(body):
        return "restatement"
    if restatement_overlap(title, body) >= 0.75:
        return "restatement"
    return "adds_detail"


def classify_kind(title: str, body: str, category: str | None = None) -> str:
    """-> 'breaking' | 'promotional' | 'standard'"""
    low = f"{title or ''} {body or ''}".lower()
    cat = (category or "").lower()

    if any(p in low for p in PROMO_MARKERS) or "promo" in cat or "marketing" in cat:
        return "promotional"
    if low.strip().startswith("breaking") or "breaking:" in low or "just in" in low:
        return "breaking"
    if "breaking" in cat or "urgent" in cat:
        return "breaking"
    return "standard"


def urgency_markers(title: str, body: str) -> list[str]:
    low = f"{title or ''} {body or ''}".lower()
    return [m for m in URGENCY_MARKERS if m in low]


def all_caps_ratio(title: str) -> float:
    letters = [c for c in (title or "") if c.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for c in letters if c.isupper()) / len(letters)


def analyze_alert(title: str, body: str, category: str | None = None) -> dict:
    title = title or ""
    body = body or ""
    markers = urgency_markers(title, body)
    return {
        "title_chars": len(title),
        "body_chars": len(body),
        "total_chars": len(title) + len(body),
        "title_words": len(title.split()),
        "framing": classify_framing(title, body),
        "kind": classify_kind(title, body, category),
        "urgency_markers": markers,
        "urgency_count": len(markers),
        "has_emoji": has_emoji(f"{title} {body}"),
        "emoji": emoji_list(f"{title} {body}"),
        "all_caps_ratio": round(all_caps_ratio(title), 3),
        "shouty": all_caps_ratio(title) > 0.7 and len(title) > 12,
        "question": "?" in title,
        "restatement_overlap": round(restatement_overlap(title, body), 3),
    }


def summarize(rows) -> dict:
    """Aggregate per-outlet copy statistics from alert rows.

    `rows` is any iterable of mappings with outlet/title/body/category.
    """
    per: dict[str, dict] = {}
    for r in rows:
        outlet = r["outlet"] or "unknown"
        a = analyze_alert(r["title"], r["body"], r["category"] if "category" in r.keys() else None)
        d = per.setdefault(
            outlet,
            {
                "outlet": outlet,
                "n": 0,
                "title_chars": [],
                "framing": Counter(),
                "kind": Counter(),
                "emoji": 0,
                "shouty": 0,
                "questions": 0,
                "urgency": 0,
                "emoji_used": Counter(),
            },
        )
        d["n"] += 1
        d["title_chars"].append(a["title_chars"])
        d["framing"][a["framing"]] += 1
        d["kind"][a["kind"]] += 1
        d["emoji"] += 1 if a["has_emoji"] else 0
        d["shouty"] += 1 if a["shouty"] else 0
        d["questions"] += 1 if a["question"] else 0
        d["urgency"] += a["urgency_count"]
        d["emoji_used"].update(a["emoji"])

    out = []
    for outlet, d in per.items():
        n = d["n"] or 1
        lens = sorted(d["title_chars"])
        out.append(
            {
                "outlet": outlet,
                "n": d["n"],
                "median_title_chars": lens[len(lens) // 2] if lens else 0,
                "mean_title_chars": round(sum(lens) / n, 1),
                "reason_to_open_pct": round(
                    100 * (d["framing"]["adds_detail"] + d["framing"]["hook"]) / n, 1
                ),
                "restatement_pct": round(100 * d["framing"]["restatement"] / n, 1),
                "hook_pct": round(100 * d["framing"]["hook"] / n, 1),
                "breaking_pct": round(100 * d["kind"]["breaking"] / n, 1),
                "promotional_pct": round(100 * d["kind"]["promotional"] / n, 1),
                "emoji_pct": round(100 * d["emoji"] / n, 1),
                "shouty_pct": round(100 * d["shouty"] / n, 1),
                "question_pct": round(100 * d["questions"] / n, 1),
                "urgency_per_alert": round(d["urgency"] / n, 2),
                "top_emoji": [e for e, _ in d["emoji_used"].most_common(5)],
            }
        )
    out.sort(key=lambda x: -x["n"])
    return {"outlets": out}
