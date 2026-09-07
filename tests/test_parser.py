"""Unit tests for the dumpsys notification parser.

These run with no emulator, no adb, no network.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from parser import _decode_extra, parse_dumpsys  # noqa: E402

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def sample():
    return parse_dumpsys(load("dumpsys_notification_sample.txt"))


# --------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------

def test_finds_every_record(sample):
    assert len(sample) == 5
    assert [n.package for n in sample] == [
        "com.cnn.mobile.android.phone",
        "com.abc.abcnews",
        "com.nytimes.android",
        "com.theathletic",
        "com.foxnews.android",
    ]


def test_title_and_body(sample):
    cnn = sample[0]
    assert cnn.title == "Supreme Court rules on tariff case"
    assert cnn.body == (
        "The 6-3 decision limits presidential authority over emergency trade powers."
    )


def test_post_time_prefers_post_time_field(sample):
    cnn = sample[0]
    assert cnn.post_time_ms == 1756825200000
    assert cnn.post_time_source == "postTime"


def test_header_fields(sample):
    abc = sample[1]
    assert abc.notif_id == "42"
    assert abc.tag == "breaking"
    assert abc.user == "0"
    assert abc.importance == "4"
    assert abc.key == "0|com.abc.abcnews|42|breaking|10241"


def test_channel_extracted_from_notification_channel_blob(sample):
    assert sample[1].channel == "news_alerts"
    assert sample[2].channel == "Breaking%20News"


def test_category_parsed_when_present(sample):
    assert sample[3].category == "promo"
    assert sample[0].category is None  # `category=null` must not become "null"


# --------------------------------------------------------------------------
# The two genuinely hard parts
# --------------------------------------------------------------------------

def test_multiline_bigtext_is_reassembled(sample):
    """android.bigText spans two lines in the fixture; both must survive."""
    abc = sample[1]
    assert abc.body is not None
    assert "exceeded authority under IEEPA." in abc.body
    assert "sets up a refund fight." in abc.body
    assert "\n" in abc.body
    # The multi-line value must not have swallowed the following extras.
    assert abc.extras["android.template"] == "android.app.Notification$BigTextStyle"


def test_redacted_dump_is_flagged_not_recorded_as_text():
    recs = parse_dumpsys(load("dumpsys_redacted.txt"))
    assert len(recs) == 0 or all(r.redacted for r in recs)
    # Crucially, "34 chars" must never become a headline.
    for r in recs:
        assert r.title != "34 chars"


def test_decode_extra_variants():
    assert _decode_extra("String (Hello world)") == ("Hello world", False)
    assert _decode_extra("SpannableString (Hi)") == ("Hi", False)
    assert _decode_extra("null") == (None, False)
    assert _decode_extra("String (10 chars)") == (None, True)
    # Nested parens inside the value must not truncate it.
    assert _decode_extra("String (Fed cuts rates (again))") == (
        "Fed cuts rates (again)",
        False,
    )
    # Untyped bare value.
    assert _decode_extra("Breaking news") == ("Breaking news", False)


# --------------------------------------------------------------------------
# Sections
# --------------------------------------------------------------------------

def test_historical_section_is_tagged(sample):
    fox = sample[4]
    assert fox.section == "historical"
    assert sample[0].section == "live"


def test_historical_can_be_excluded():
    recs = parse_dumpsys(load("dumpsys_notification_sample.txt"), include_historical=False)
    assert all(r.section == "live" for r in recs)
    assert len(recs) == 4


# --------------------------------------------------------------------------
# Robustness
# --------------------------------------------------------------------------

def test_empty_dump_returns_nothing():
    assert parse_dumpsys(load("dumpsys_empty.txt")) == []


def test_blank_and_garbage_input_do_not_raise():
    assert parse_dumpsys("") == []
    assert parse_dumpsys("not a dump at all\n\n\x00\xff") == []


def test_malformed_dump_still_yields_good_records():
    recs = parse_dumpsys(load("dumpsys_malformed.txt"))
    pkgs = [r.package for r in recs]
    # The record with no pkg= is dropped; the AP record survives.
    assert "com.apnews" in pkgs
    # The title-less systemui record carries no signal and is dropped.
    assert "com.android.systemui" not in pkgs
    ap = next(r for r in recs if r.package == "com.apnews")
    assert ap.title == "Court rules against emergency tariffs"
    assert ap.post_time_ms == 1756825080000


def test_unparseable_timestamp_falls_back_to_none():
    recs = parse_dumpsys(load("dumpsys_malformed.txt"))
    broken = [r for r in recs if r.package == "com.example.brokenapp"]
    for r in broken:
        assert r.post_time_ms is None


def test_zero_timestamps_are_rejected():
    dump = """
  Notification List:
    NotificationRecord(0x1: pkg=com.example.x user=0 id=1 tag=null importance=3 key=k)
      postTime=0
      when=0
      mCreationTimeMs=1756825200000
      Notification.extras={
        android.title=String (Hi)
      }
"""
    rec = parse_dumpsys(dump)[0]
    assert rec.post_time_ms == 1756825200000
    assert rec.post_time_source == "mCreationTimeMs"


def test_url_is_lifted_from_body():
    recs = parse_dumpsys(load("dumpsys_notification_sample.txt"))
    athletic = next(r for r in recs if r.package == "com.theathletic")
    assert athletic.url == "https://theathletic.com/offer"


def test_dedupe_key_shape(sample):
    pkg, title, ts = sample[0].dedupe_key
    assert pkg == "com.cnn.mobile.android.phone"
    assert title == "Supreme Court rules on tariff case"
    assert ts == 1756825200000
