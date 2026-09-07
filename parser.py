"""Parser for `adb shell dumpsys notification --noredact` output.

WHY THIS IS WRITTEN THE WAY IT IS
---------------------------------
`dumpsys notification` output is produced by NotificationRecord.dump() /
NotificationManagerService.dumpImpl() in AOSP. It is a human-readable debug
dump, not a stable API. It differs between Android versions, and OEM builds
add fields. So this parser is deliberately *tolerant*:

  * Records are delimited by lines containing ``NotificationRecord(``.
  * Everything between one record header and the next belongs to that record.
  * Fields are found by name anywhere inside the record block, not by fixed
    line offsets.
  * A field we do not recognise is ignored, never fatal.
  * Any single unparseable record is skipped; the rest of the dump still parses.

The two genuinely fiddly parts, both handled below:

1. **Extras values span lines.** The dump prints extras as
   ``android.text=String (some text)``. If the text itself contains a newline
   (very common for ``android.bigText``), the value continues on following
   lines. We therefore accumulate until parentheses balance, rather than
   assuming one field per line.

2. **Redaction.** Without ``--noredact`` the platform prints
   ``android.title=String (10 chars)`` instead of the content. We detect that
   shape and flag it, so a misconfigured capture is loud rather than silently
   recording the literal string "10 chars" as a headline.

FORMAT PROVENANCE / HONESTY NOTE
--------------------------------
The fixtures in tests/fixtures/ were reconstructed from the AOSP dump format,
not copied from a live emulator (no Android SDK was installed on the build
machine). The parser is written to tolerate variation, and `capture.py
--save-raw` exists precisely so the first real dump can be diffed against the
fixtures. See README "Validating the parser against a real device".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterator

# --------------------------------------------------------------------------
# Section headers in the dump. Records appearing under a "historical" heading
# have already been dismissed/cancelled -- capturing them is a partial
# mitigation for the self-cancelling-notification miss described in the brief.
# --------------------------------------------------------------------------
_SECTION_PATTERNS = (
    (re.compile(r"^\s*Notification List:", re.I), "live"),
    (re.compile(r"^\s*Live Notifications", re.I), "live"),
    (re.compile(r"^\s*Historical Notifications", re.I), "historical"),
    (re.compile(r"^\s*Snoozed Notifications", re.I), "snoozed"),
)

_RECORD_START = re.compile(r"NotificationRecord\(")

# Header line, e.g.
#   NotificationRecord(0xabc123: pkg=com.cnn.mobile.android.phone user=0 \
#     id=1001 tag=null importance=4 key=0|com.cnn...|1001|null|10234: ...)
_HDR_PKG = re.compile(r"\bpkg=(\S+)")
_HDR_USER = re.compile(r"\buser=(-?\d+)")
_HDR_ID = re.compile(r"\bid=(-?\d+)")
_HDR_TAG = re.compile(r"\btag=(\S+?)(?=\s|\))")
_HDR_KEY = re.compile(r"\bkey=(\S+?)(?=[:\s)]|$)")
_HDR_IMPORTANCE = re.compile(r"\bimportance=(\S+)")

# Standalone `field=value` lines inside the record body.
_KV_LINE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_.]*)=(.*)$")

# Timestamps, in the precedence order we trust them.
# postTime is StatusBarNotification.getPostTime() -- exactly what the brief
# wants. mCreationTimeMs is NotificationRecord's own creation stamp and is
# effectively the same instant for a freshly posted alert. `when` is the
# app-supplied timestamp on the Notification and is the least trustworthy
# (apps sometimes set it to the article publish time, or to 0).
_TIME_FIELDS = ("postTime", "mCreationTimeMs", "mUpdateTimeMs", "when")

# `android.title=String (Foo)` -> group 1 = java type, group 2 = open paren
_EXTRA_TYPED = re.compile(r"^([A-Za-z][A-Za-z0-9_.$\[\]]*)\s+\((.*)$", re.S)

# Redacted form emitted when --noredact is absent.
_REDACTED = re.compile(r"^\d+\s+chars$")

# Extras we care about, mapped to a normalised name.
_EXTRA_KEYS = {
    "android.title": "title",
    "android.title.big": "title_big",
    "android.text": "text",
    "android.bigText": "big_text",
    "android.subText": "sub_text",
    "android.summaryText": "summary_text",
    "android.infoText": "info_text",
    "android.template": "template",
}

_URL_RE = re.compile(r"https?://[^\s\"'<>)\]]+")


@dataclass
class ParsedNotification:
    """One notification lifted out of a dumpsys dump."""

    package: str
    title: str | None = None
    body: str | None = None
    post_time_ms: int | None = None
    post_time_source: str | None = None
    key: str | None = None
    notif_id: str | None = None
    tag: str | None = None
    user: str | None = None
    channel: str | None = None
    category: str | None = None
    importance: str | None = None
    section: str = "live"
    url: str | None = None
    redacted: bool = False
    extras: dict[str, str] = field(default_factory=dict)
    fields: dict[str, str] = field(default_factory=dict)

    @property
    def dedupe_key(self) -> tuple[str, str, int | None]:
        """The brief's dedupe tuple: (pkg, title, postTime)."""
        return (self.package, self.title or "", self.post_time_ms)

    def to_raw(self) -> dict[str, Any]:
        return {
            "package": self.package,
            "key": self.key,
            "id": self.notif_id,
            "tag": self.tag,
            "user": self.user,
            "channel": self.channel,
            "category": self.category,
            "importance": self.importance,
            "section": self.section,
            "post_time_ms": self.post_time_ms,
            "post_time_source": self.post_time_source,
            "redacted": self.redacted,
            "extras": self.extras,
            "fields": {k: v for k, v in self.fields.items() if k in _TIME_FIELDS},
        }


# --------------------------------------------------------------------------
# Record splitting
# --------------------------------------------------------------------------

def _split_records(text: str) -> Iterator[tuple[str, list[str]]]:
    """Yield (section, lines) for each NotificationRecord block in the dump."""
    section = "live"
    current: list[str] | None = None
    current_section = "live"

    for line in text.splitlines():
        for pat, name in _SECTION_PATTERNS:
            if pat.match(line):
                # A section header ends the record that preceded it.
                if current:
                    yield current_section, current
                    current = None
                section = name
                break

        if _RECORD_START.search(line):
            if current:
                yield current_section, current
            current = [line]
            current_section = section
            continue

        if current is not None:
            current.append(line)

    if current:
        yield current_section, current


# --------------------------------------------------------------------------
# Value decoding
# --------------------------------------------------------------------------

def _balanced_end(s: str) -> int | None:
    """Index just past the ')' that closes the paren opened before `s`.

    `s` is the text following the opening '(' of a typed extra value. Returns
    None if the parens never balance within `s` (value continues on the next
    line).
    """
    depth = 1
    for i, ch in enumerate(s):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
    return None


def _decode_extra(raw: str) -> tuple[str | None, bool]:
    """Decode an extras value. Returns (value, was_redacted).

    Handles:
      ``String (Breaking news)``       -> "Breaking news"
      ``SpannableString (Hi there)``   -> "Hi there"
      ``String (10 chars)``            -> (None, True)     [redacted dump]
      ``null``                         -> (None, False)
      ``Breaking news``                -> "Breaking news"  [untyped variant]
    """
    raw = raw.strip()
    if not raw or raw == "null":
        return None, False

    m = _EXTRA_TYPED.match(raw)
    if m:
        inner = m.group(2)
        end = _balanced_end(inner)
        value = inner[:end] if end is not None else inner
        value = value.strip()
        if _REDACTED.match(value):
            return None, True
        return (value or None), False

    # Untyped: some builds print the bare value.
    return raw, False


def _is_new_field(line: str) -> bool:
    """True if `line` looks like the start of a new key=value, not a continuation."""
    m = _KV_LINE.match(line)
    if not m:
        return False
    # A continuation line of free text can coincidentally contain '='. Require
    # the key to look like a dotted/simple identifier with no spaces, which the
    # regex already enforces, and that the line is not deeply wrapped prose.
    return True


def _collect_fields(lines: list[str]) -> tuple[dict[str, str], dict[str, str], bool]:
    """Walk a record body, returning (extras, plain_fields, redacted_flag).

    Multi-line extras values are joined back together.
    """
    extras: dict[str, str] = {}
    plain: dict[str, str] = {}
    redacted = False

    i = 0
    n = len(lines)
    while i < n:
        m = _KV_LINE.match(lines[i])
        if not m:
            i += 1
            continue
        key, rest = m.group(1), m.group(2)

        # If this is a typed value whose parens do not close on this line,
        # keep consuming lines until they do (bounded, so a malformed dump
        # cannot swallow the whole record).
        typed = _EXTRA_TYPED.match(rest.strip())
        if typed and _balanced_end(typed.group(2)) is None:
            buf = [rest]
            j = i + 1
            consumed = 0
            while j < n and consumed < 200:
                buf.append(lines[j])
                joined = "\n".join(buf).strip()
                t2 = _EXTRA_TYPED.match(joined)
                if t2 and _balanced_end(t2.group(2)) is not None:
                    break
                # Safety valve: a new record header means the value never
                # closed; bail out rather than devour the next notification.
                if _RECORD_START.search(lines[j]):
                    buf.pop()
                    break
                j += 1
                consumed += 1
            rest = "\n".join(buf)
            i = j
        # else: single-line value

        if key in _EXTRA_KEYS or key.startswith("android."):
            value, was_redacted = _decode_extra(rest)
            redacted = redacted or was_redacted
            if value is not None:
                extras[key] = value
        else:
            value = rest.strip()
            if value and value != "null":
                plain.setdefault(key, value)
        i += 1

    return extras, plain, redacted


def _clean_tag(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip().rstrip(":").rstrip(")")
    return None if value in ("", "null") else value


def _extract_channel(plain: dict[str, str]) -> str | None:
    """Channel is printed either bare or as NotificationChannel{mId='x', ...}."""
    raw = plain.get("channel") or plain.get("channelId")
    if not raw:
        return None
    m = re.search(r"mId='([^']*)'", raw)
    if m:
        return m.group(1)
    # `channel=breaking_news` or `channel=breaking shortcut=null ...`
    return raw.split()[0].strip("{}").strip() or None


def _pick_time(plain: dict[str, str]) -> tuple[int | None, str | None]:
    for name in _TIME_FIELDS:
        raw = plain.get(name)
        if not raw:
            continue
        m = re.match(r"^(-?\d+)", raw.strip())
        if not m:
            continue
        val = int(m.group(1))
        # `when=0` is the "app didn't set it" sentinel; skip it.
        if val <= 0:
            continue
        # Sanity: reject anything before 2000-01-01 or absurdly far ahead.
        if val < 946_684_800_000 or val > 4_102_444_800_000:
            continue
        return val, name
    return None, None


def parse_record(section: str, lines: list[str]) -> ParsedNotification | None:
    """Parse one NotificationRecord block. Returns None if unusable."""
    header = lines[0]
    pkg_m = _HDR_PKG.search(header)
    if not pkg_m:
        # Some builds put pkg on the following line.
        joined = "\n".join(lines[:4])
        pkg_m = _HDR_PKG.search(joined)
    if not pkg_m:
        return None
    package = pkg_m.group(1).strip().rstrip(":,")
    if not package or package == "null":
        return None

    extras, plain, redacted = _collect_fields(lines[1:])

    title = extras.get("android.title") or extras.get("android.title.big")
    text = (
        extras.get("android.text")
        or extras.get("android.bigText")
        or extras.get("android.summaryText")
    )
    body = extras.get("android.bigText") or extras.get("android.text") or None

    post_ms, post_src = _pick_time(plain)

    key_m = _HDR_KEY.search(header)
    tag_m = _HDR_TAG.search(header)
    id_m = _HDR_ID.search(header)
    user_m = _HDR_USER.search(header)
    imp_m = _HDR_IMPORTANCE.search(header)

    # URL: apps rarely put one in extras, but some do, and the intent dump
    # sometimes carries a deep link. Look across the whole record.
    url = None
    url_m = _URL_RE.search("\n".join(lines))
    if url_m:
        url = url_m.group(0).rstrip(".,;)")

    return ParsedNotification(
        package=package,
        title=title,
        body=body if body != title else (text if text != title else None),
        post_time_ms=post_ms,
        post_time_source=post_src,
        key=_clean_tag(key_m.group(1)) if key_m else None,
        notif_id=id_m.group(1) if id_m else None,
        tag=_clean_tag(tag_m.group(1)) if tag_m else None,
        user=user_m.group(1) if user_m else None,
        channel=_extract_channel(plain),
        category=_clean_tag(plain.get("category")),
        importance=_clean_tag(imp_m.group(1)) if imp_m else None,
        section=section,
        url=url,
        redacted=redacted,
        extras=extras,
        fields=plain,
    )


def parse_dumpsys(text: str, include_historical: bool = True) -> list[ParsedNotification]:
    """Parse a full `dumpsys notification --noredact` dump.

    Never raises on malformed input: bad records are skipped.
    """
    out: list[ParsedNotification] = []
    if not text:
        return out
    for section, lines in _split_records(text):
        if section != "live" and not include_historical:
            continue
        try:
            rec = parse_record(section, lines)
        except Exception:  # pragma: no cover - defensive; a dump must never crash capture
            continue
        if rec is None:
            continue
        # A record with neither title nor body carries no signal.
        if not rec.title and not rec.body:
            continue
        out.append(rec)
    return out
