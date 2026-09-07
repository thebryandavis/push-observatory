"""observe.py against fixture XML and canned adb output. No device anywhere.

The contract: every extraction is exercised on realistic uiautomator dumps
(Switch / SwitchCompat / MaterialSwitch / CheckBox, checked true and false,
disabled rows, permission-controller dialogs), every adb failure mode gives a
message a person can act on, and a full session round-trips to disk with an
index.md that says what happened -- including when a step failed.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import observe  # noqa: E402

FIX = ROOT / "tests" / "fixtures"
SETTINGS_XML = (FIX / "uiautomator_alert_settings.xml").read_text()
PREPROMPT_XML = (FIX / "uiautomator_preprompt.xml").read_text()
OS_PROMPT_XML = (FIX / "uiautomator_os_prompt.xml").read_text()
TRUNCATED_XML = (FIX / "uiautomator_truncated.xml").read_text()
CHANNELS_DUMP = (FIX / "dumpsys_notification_channels_api34.txt").read_text()

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


# --------------------------------------------------------------------------
# UI extraction
# --------------------------------------------------------------------------

def test_toggles_with_checked_state_and_labels():
    sm = observe.summarize_ui(observe.parse_ui_xml(SETTINGS_XML))
    by_label = {t["label"]: t for t in sm["toggles"]}
    assert set(by_label) == {"Breaking News", "Top Stories", "Politics", "Local News", "Daily briefing toggle"}
    assert by_label["Breaking News"]["checked"] is True
    assert by_label["Top Stories"]["checked"] is False
    assert by_label["Politics"]["checked"] is False and by_label["Politics"]["class"] == "CheckBox"
    assert by_label["Local News"]["enabled"] is False, "disabled row must be visible as disabled"
    assert by_label["Daily briefing toggle"]["checked"] is True
    assert by_label["Daily briefing toggle"]["class"] == "MaterialSwitch"


def test_toggle_label_falls_back_to_preceding_text():
    # Top Stories' Switch has no text of its own; the label is the sibling TextView.
    sm = observe.summarize_ui(observe.parse_ui_xml(SETTINGS_XML))
    top = next(t for t in sm["toggles"] if t["resource_id"].endswith("switch_top"))
    assert top["label"] == "Top Stories"


def test_texts_buttons_and_packages():
    sm = observe.summarize_ui(observe.parse_ui_xml(SETTINGS_XML))
    assert "Sign in to set your location" in sm["texts"]
    assert "Every morning at 7:00 AM" in sm["texts"]
    assert [b["label"] for b in sm["buttons"]] == ["Navigate up", "Manage in system settings"]
    assert sm["packages_on_screen"] == ["com.abc.abcnews"]
    assert sm["node_count"] > 10


def test_preprompt_copy_is_captured_verbatim():
    sm = observe.summarize_ui(observe.parse_ui_xml(PREPROMPT_XML))
    assert "Get breaking news alerts as they happen. You can change this any time in Settings." in sm["texts"]
    labels = [b["label"] for b in sm["buttons"]]
    assert "Turn on notifications" in labels and "Not now" in labels
    assert sm["editable"] == ["Email address"]
    assert sm["toggles"] == []


def test_os_prompt_is_recognised_as_permission_controller():
    sm = observe.summarize_ui(observe.parse_ui_xml(OS_PROMPT_XML))
    assert sm["packages_on_screen"] == ["com.android.permissioncontroller"]
    assert "Allow ABC News to send you notifications?" in sm["texts"]
    assert {b["label"] for b in sm["buttons"]} == {"Allow", "Don’t allow"}


def test_truncated_xml_raises_not_empty():
    with pytest.raises(observe.AdbError, match="malformed"):
        observe.parse_ui_xml(TRUNCATED_XML)


def test_empty_hierarchy_is_an_empty_screen_not_an_error():
    sm = observe.summarize_ui(observe.parse_ui_xml('<hierarchy rotation="0"></hierarchy>'))
    assert sm["texts"] == [] and sm["toggles"] == [] and sm["node_count"] == 0


# --------------------------------------------------------------------------
# Notification channels
# --------------------------------------------------------------------------

def test_channels_scoped_to_package():
    r = observe.parse_channels(CHANNELS_DUMP, "com.abc.abcnews")
    assert r["scoped"] is True
    assert r["permission"] == "allowed" and r["app_importance"] == "DEFAULT"
    ids = [c["id"] for c in r["channels"]]
    assert ids == ["breaking_news", "top_stories", "promo", "fcm_fallback_notification_channel"]
    assert "breaking" not in ids, "CNN's channel must not leak into ABC's block"
    assert "breaking-news" not in ids, "NYT's channel must not leak into ABC's block"
    by = {c["id"]: c for c in r["channels"]}
    assert by["breaking_news"]["importance"] == 4 and by["breaking_news"]["importance_label"] == "HIGH"
    assert by["breaking_news"]["group"] == "news"
    assert by["breaking_news"]["description"] == "Alerts for major breaking stories"
    assert by["promo"]["blocked"] is True and r["blocked"] == ["promo"]
    assert by["promo"]["user_locked"] == "4"
    assert by["fcm_fallback_notification_channel"]["deleted"] is True
    assert by["top_stories"]["description"] == "The day's most important stories"


def test_channels_app_level_block_detected():
    r = observe.parse_channels(CHANNELS_DUMP, "com.nytimes.android")
    assert r["permission"] == "blocked"
    assert [c["id"] for c in r["channels"]] == ["breaking-news"]


def test_channels_missing_package_falls_back_unscoped():
    r = observe.parse_channels(CHANNELS_DUMP, "com.example.notinstalled")
    assert r["scoped"] is False
    assert r["permission"] is None
    assert len(r["channels"]) == 6, "unscoped fallback returns every channel line in the dump"


def test_channel_line_with_quoted_commas():
    line = ("NotificationChannel{mId='a,b', mName=Weather, and traffic, mDescription=null, "
            "mImportance=2, mGroup='null'}")
    c = observe.parse_channel_line(line)
    assert c["id"] == "a,b" and c["importance_label"] == "LOW" and c["group"] == ""
    assert observe.parse_channel_line("NotificationRecord(0x1: pkg=x)") is None


# --------------------------------------------------------------------------
# adb wrapper against canned processes
# --------------------------------------------------------------------------

def cp(stdout=b"", stderr=b"", rc=0):
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr=stderr)


class FakeRunner:
    """Maps the adb argv tail to a canned response; records every call."""

    def __init__(self, table):
        self.table = table
        self.calls = []

    def __call__(self, cmd, timeout):
        self.calls.append(cmd)
        args = cmd[1:]
        if args[:1] == ["-s"]:
            args = args[2:]
        tail = " ".join(args)
        for key, resp in self.table.items():
            if tail.startswith(key):
                if isinstance(resp, Exception):
                    raise resp
                return resp
        return cp(stderr=b"unexpected: " + tail.encode(), rc=1)


def test_no_adb_binary_is_actionable():
    adb = observe.Adb(None)
    with pytest.raises(observe.AdbError, match="adb not found.*HUMAN STEP 1"):
        adb.require_device()


def test_no_device_is_actionable():
    adb = observe.Adb("/fake/adb", runner=FakeRunner({"devices": cp(b"List of devices attached\n\n")}))
    with pytest.raises(observe.AdbError, match="no device/emulator connected.*emulator -avd"):
        adb.require_device()


def test_offline_device_is_explained():
    adb = observe.Adb("/fake/adb", runner=FakeRunner({"devices": cp(b"List of devices attached\nemulator-5554\toffline\n")}))
    with pytest.raises(observe.AdbError, match="offline.*still booting"):
        adb.require_device()


def test_ready_device_is_selected():
    adb = observe.Adb("/fake/adb", runner=FakeRunner({
        "devices": cp(b"List of devices attached\nemulator-5554\tdevice\nemulator-5556\toffline\n")}))
    assert adb.require_device() == "emulator-5554"
    assert adb.serial == "emulator-5554"


def test_explicit_serial_not_connected():
    adb = observe.Adb("/fake/adb", serial="emulator-5560",
                      runner=FakeRunner({"devices": cp(b"List of devices attached\nemulator-5554\tdevice\n")}))
    with pytest.raises(observe.AdbError, match="emulator-5560 is not connected"):
        adb.require_device()


def test_adb_timeout_and_oserror_become_adberror():
    adb = observe.Adb("/fake/adb", runner=FakeRunner({"devices": subprocess.TimeoutExpired("adb", 20)}))
    with pytest.raises(observe.AdbError, match="timed out"):
        adb.devices()
    adb = observe.Adb("/fake/adb", runner=FakeRunner({"devices": OSError("exec format error")}))
    with pytest.raises(observe.AdbError, match="could not execute adb"):
        adb.devices()


def test_screencap_rejects_non_png():
    adb = observe.Adb("/fake/adb", runner=FakeRunner({"exec-out screencap -p": cp(b"error: closed\n")}))
    with pytest.raises(observe.AdbError, match="not a PNG"):
        observe.screencap(adb)


def test_ui_dump_reads_file_back():
    r = FakeRunner({
        "shell uiautomator dump": cp(b"UI hierchary dumped to: /sdcard/window_dump.xml\n"),
        "exec-out cat /sdcard/window_dump.xml": cp(SETTINGS_XML.encode()),
    })
    adb = observe.Adb("/fake/adb", runner=r)
    assert "<hierarchy" in observe.ui_dump(adb)
    assert r.calls[0][1:] == ["shell", "uiautomator", "dump", observe.DEVICE_DUMP_PATH]


def test_ui_dump_secure_window_is_explained():
    r = FakeRunner({"shell uiautomator dump": cp(b"ERROR: could not get idle state.\n"),
                    "exec-out cat": cp(b"")})
    with pytest.raises(observe.AdbError, match="uiautomator dump failed"):
        observe.ui_dump(observe.Adb("/fake/adb", runner=r))


def test_launch_uses_monkey_and_detects_missing_app():
    r = FakeRunner({"shell monkey": cp(b"Events injected: 1\n")})
    observe.launch_app(observe.Adb("/fake/adb", runner=r), "com.abc.abcnews")
    assert r.calls[-1][1:] == ["shell", "monkey", "-p", "com.abc.abcnews",
                               "-c", "android.intent.category.LAUNCHER", "1"]
    r = FakeRunner({"shell monkey": cp(b"** No activities found to run, monkey aborted.\n")})
    with pytest.raises(observe.AdbError, match="no launcher activity"):
        observe.launch_app(observe.Adb("/fake/adb", runner=r), "com.abc.abcnews")


def test_open_notification_settings_intent():
    r = FakeRunner({"shell am start": cp(b"Starting: Intent { act=android.settings.APP_NOTIFICATION_SETTINGS }\n")})
    observe.open_notification_settings(observe.Adb("/fake/adb", runner=r), "com.abc.abcnews")
    assert r.calls[-1][1:] == ["shell", "am", "start", "-a", "android.settings.APP_NOTIFICATION_SETTINGS",
                               "--es", "android.provider.extra.APP_PACKAGE", "com.abc.abcnews"]


def test_fetch_channels_prefers_dumpsys_and_tolerates_missing_cmd():
    r = FakeRunner({
        "shell dumpsys notification --noredact": cp(CHANNELS_DUMP.encode()),
        "shell cmd notification list_channels": cp(b"Error: Unknown command: list_channels\n", rc=255),
    })
    result, raw = observe.fetch_channels(observe.Adb("/fake/adb", runner=r), "com.abc.abcnews")
    assert result["source"] == "dumpsys notification"
    assert len(result["channels"]) == 4 and "cmd_list_channels" not in result
    assert raw == CHANNELS_DUMP


def test_fetch_channels_falls_back_when_noredact_unsupported():
    r = FakeRunner({
        "shell dumpsys notification --noredact": cp(b"Unknown argument: --noredact\n", rc=1),
        "shell dumpsys notification": cp(CHANNELS_DUMP.encode()),
        "shell cmd notification": cp(b"", rc=1),
    })
    result, _ = observe.fetch_channels(observe.Adb("/fake/adb", runner=r), "com.abc.abcnews")
    assert len(result["channels"]) == 4


def test_installed_packages_parses_pm_list():
    r = FakeRunner({"shell pm list packages -3": cp(b"package:com.abc.abcnews\npackage:com.cnn.mobile.android.phone\n")})
    assert observe.installed_packages(observe.Adb("/fake/adb", runner=r)) == [
        "com.abc.abcnews", "com.cnn.mobile.android.phone"]


# --------------------------------------------------------------------------
# Session round-trip on disk
# --------------------------------------------------------------------------

def good_runner(xml=SETTINGS_XML):
    return FakeRunner({
        "devices": cp(b"List of devices attached\nemulator-5554\tdevice\n"),
        "exec-out screencap -p": cp(PNG),
        "shell uiautomator dump": cp(b"UI hierchary dumped to: /sdcard/window_dump.xml\n"),
        "exec-out cat /sdcard/window_dump.xml": cp(xml.encode()),
        "shell monkey": cp(b"Events injected: 1\n"),
        "shell am start": cp(b"Starting: Intent\n"),
        "shell dumpsys notification --noredact": cp(CHANNELS_DUMP.encode()),
        "shell cmd notification": cp(b"", rc=1),
    })


def test_session_writes_numbered_files_and_index(tmp_path):
    s = observe.Session("com.abc.abcnews", "first install", str(tmp_path), date="2026-09-05")
    adb = observe.Adb("/fake/adb", runner=good_runner())
    st = s.capture(adb, "alert-settings")
    assert st.ok and st.number == 1
    d = tmp_path / "com.abc.abcnews" / "2026-09-05"
    assert (d / "01-alert-settings.png").read_bytes() == PNG
    assert "<hierarchy" in (d / "01-alert-settings.xml").read_text()
    m = json.loads((d / "steps.json").read_text())
    assert m["sessions"] == ["first install"] and len(m["steps"]) == 1
    idx = (d / "index.md").read_text()
    assert "## 01 — alert-settings" in idx
    assert "| ON | Breaking News | Switch |" in idx
    assert "| off | Top Stories | SwitchCompat |" in idx
    assert "| off (disabled) | Local News |" in idx
    assert "![alert-settings](01-alert-settings.png)" in idx


def test_numbering_continues_across_sessions_same_day(tmp_path):
    adb = observe.Adb("/fake/adb", runner=good_runner())
    s1 = observe.Session("com.abc.abcnews", "morning", str(tmp_path), date="2026-09-05")
    s1.capture(adb, "first-launch")
    s2 = observe.Session("com.abc.abcnews", "afternoon", str(tmp_path), date="2026-09-05")
    st = s2.capture(adb, "alert-settings")
    assert st.number == 2
    m = json.loads((tmp_path / "com.abc.abcnews" / "2026-09-05" / "steps.json").read_text())
    assert m["sessions"] == ["morning", "afternoon"]
    assert [x["label"] for x in m["steps"]] == ["first-launch", "alert-settings"]


def test_failed_step_is_recorded_not_faked(tmp_path):
    r = good_runner()
    r.table["exec-out screencap -p"] = cp(b"error: device offline\n", rc=1)
    s = observe.Session("com.abc.abcnews", "", str(tmp_path), date="2026-09-05")
    st = s.capture(observe.Adb("/fake/adb", runner=r), "first-launch")
    assert st.ok is False and "device offline" in st.error
    assert st.png is None
    idx = (tmp_path / "com.abc.abcnews" / "2026-09-05" / "index.md").read_text()
    assert "CAPTURE FAILED" in idx and "device offline" in idx
    assert "1 failed" in idx


def test_record_channels_and_index_section(tmp_path):
    s = observe.Session("com.abc.abcnews", "", str(tmp_path), date="2026-09-05")
    result, _ = observe.fetch_channels(observe.Adb("/fake/adb", runner=good_runner()), "com.abc.abcnews")
    s.record_channels(result)
    d = tmp_path / "com.abc.abcnews" / "2026-09-05"
    assert json.loads((d / "channels.json").read_text())["channels"][0]["id"] == "breaking_news"
    idx = (d / "index.md").read_text()
    assert "## OS notification channels" in idx
    assert "| Breaking News | `breaking_news` | HIGH | news | no |" in idx
    assert "NONE (blocked)" in idx


def test_rebuild_index_from_disk(tmp_path):
    s = observe.Session("com.abc.abcnews", "", str(tmp_path), date="2026-09-05")
    s.capture(observe.Adb("/fake/adb", runner=good_runner()), "alert-settings")
    d = tmp_path / "com.abc.abcnews" / "2026-09-05"
    (d / "index.md").unlink()
    s2 = observe.Session("com.abc.abcnews", "", str(tmp_path), date="2026-09-05")
    path = s2.rebuild_index()
    assert pathlib.Path(path).read_text().count("Breaking News") >= 1


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def test_cli_no_device_exits_1_with_message(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(observe, "find_adb", lambda explicit=None: None)
    rc = observe.main(["--out", str(tmp_path), "--package", "com.example.newsapp", "--step", "first-launch"])
    assert rc == 1
    assert "adb not found" in capsys.readouterr().err


def test_cli_nothing_to_do_exits_2(tmp_path, capsys):
    assert observe.main(["--out", str(tmp_path)]) == 2
    assert "nothing to do" in capsys.readouterr().err


def test_cli_protocol_lists_every_step(capsys):
    assert observe.main(["--protocol"]) == 0
    out = capsys.readouterr().out
    for label, _ in observe.PROTOCOL_STEPS:
        assert label in out


def test_cli_full_run_with_fake_adb(tmp_path, monkeypatch, capsys):
    r = good_runner()
    monkeypatch.setattr(observe, "find_adb", lambda explicit=None: "/fake/adb")
    monkeypatch.setattr(observe, "_default_runner", r)
    rc = observe.main(["--out", str(tmp_path), "--package", "com.abc.abcnews", "--session", "t",
                       "--launch", "--step", "first-launch", "--step", "alert-settings",
                       "--notification-settings", "--wait", "0", "--date", "2026-09-05"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "01 first-launch" in out and "02 alert-settings" in out and "03 os-notification-settings" in out
    assert "channels: 4" in out
    d = tmp_path / "com.abc.abcnews" / "2026-09-05"
    assert (d / "dumpsys-notification.txt").exists()
    assert sorted(p.name for p in d.glob("*.png")) == [
        "01-first-launch.png", "02-alert-settings.png", "03-os-notification-settings.png"]
    # launch happened before the first capture
    assert any("monkey" in c for c in r.calls[:3])


def test_protocol_steps_match_document():
    doc = (ROOT.parent / "output" / "abc-news-app-observation.md").read_text()
    for label, _ in observe.PROTOCOL_STEPS:
        assert f"`{label}`" in doc, f"protocol doc does not mention step {label}"
