#!/usr/bin/env python3
"""adb-driven observation helper: document what an app does, step by step.

The Push Observatory's quantitative side records what apps *send*. This is
the qualitative side: what the app *shows* -- onboarding, the notification
pre-prompt, the OS prompt, the in-app alert settings, follow surfaces. Each
step captures a screenshot and the uiautomator view hierarchy, then extracts
every visible text and every toggle's checked state into a readable index.

Output layout (one folder per package per day; numbering continues across
sessions on the same day):

    observations/<package>/<YYYY-MM-DD>/
        01-first-launch.png
        01-first-launch.xml
        02-onboarding-1.png
        ...
        channels.json          (from --notification-settings)
        steps.json             (manifest; the dashboard reads this)
        index.md               (regenerated after every step)

Usage:
    observe.py --package <package> --session "first install" --launch \\
               --step first-launch
    observe.py --step onboarding-1 --step onboarding-2     # keeps numbering
    observe.py --interactive                               # label per prompt
    observe.py --notification-settings                     # OS channel screen
    observe.py --protocol                                  # print step labels
    observe.py --index-only                                # rebuild index.md

Every adb failure mode -- adb missing, no device, device offline, dump
garbage -- produces a specific message and a non-zero exit. Nothing here
fabricates a capture: if the screenshot or the dump did not come back, the
step is recorded as failed with the reason, and index.md says so.

Standard library only, like the capture loop, and for the same reason.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Callable

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PACKAGE = None  # required: pass --package <android package id>
DEFAULT_OUT = os.path.join(HERE, "observations")
DEVICE_DUMP_PATH = "/sdcard/window_dump.xml"

# The step labels the observation protocol in
# projects/output/abc-news-app-observation.md refers to. Kept here so the
# capture folder and the document line up by construction.
PROTOCOL_STEPS: tuple[tuple[str, str], ...] = (
    ("install", "Play Store listing at the moment of install (version, permissions)"),
    ("first-launch", "Cold start after install, before touching anything"),
    ("onboarding-1", "Each onboarding screen in order (continue with -2, -3, ...)"),
    ("notif-preprompt", "The app's own pre-prompt before the OS permission dialog"),
    ("notif-os-prompt", "The Android 13+ POST_NOTIFICATIONS system dialog"),
    ("home-feed", "The home feed as first seen; scroll once and capture -2"),
    ("home-feed-end", "After scrolling hard: does the feed end, or is it infinite?"),
    ("settings-path-1", "Every screen on the path to notification settings (-2, -3, ...)"),
    ("alert-settings", "The in-app alert settings screen, every toggle visible"),
    ("alert-settings-2", "The same screen scrolled, until nothing new appears"),
    ("alert-detail", "Any per-toggle detail or sub-screen (frequency, sound, quiet hours)"),
    ("follow-topic", "The follow/My News surface before following anything"),
    ("follow-topic-after", "Alert settings again immediately after following one topic"),
    ("location", "Any location / local-news setting; what granularity it asks for"),
    ("account-prompt", "Every sign-in or account wall encountered, wherever it appears"),
    ("os-notification-settings", "OS channel list (captured by --notification-settings)"),
)


class AdbError(Exception):
    """Anything that stops a capture. The message is meant for a human."""


# ---------------------------------------------------------------------------
# adb plumbing
# ---------------------------------------------------------------------------

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
    for var in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        sdk = os.environ.get(var)
        if sdk:
            candidates.insert(0, os.path.join(sdk, "platform-tools", "adb"))
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


Runner = Callable[[list[str], int], subprocess.CompletedProcess]


def _default_runner(cmd: list[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, timeout=timeout)


class Adb:
    """A thin adb wrapper. `runner` is injectable so every code path can be
    exercised against canned output with no device attached."""

    def __init__(self, path: str | None, serial: str | None = None,
                 runner: Runner | None = None):
        self.path = path
        self.serial = serial
        self._run = runner or _default_runner

    def _base(self) -> list[str]:
        if not self.path:
            raise AdbError(
                "adb not found. Install the Android SDK platform-tools (README, "
                "HUMAN STEP 1), add them to PATH, or pass --adb /path/to/adb."
            )
        cmd = [self.path]
        if self.serial:
            cmd += ["-s", self.serial]
        return cmd

    def run(self, args: list[str], timeout: int = 45) -> subprocess.CompletedProcess:
        cmd = self._base() + args
        try:
            return self._run(cmd, timeout)
        except subprocess.TimeoutExpired as e:
            raise AdbError(f"adb timed out after {timeout}s: {' '.join(args)}") from e
        except OSError as e:
            raise AdbError(f"could not execute adb at {self.path}: {e}") from e

    def devices(self) -> list[tuple[str, str]]:
        """[(serial, state)] from `adb devices`. Never raises for an empty list."""
        p = self.run(["devices"], timeout=20)
        if p.returncode != 0:
            raise AdbError(f"`adb devices` exit {p.returncode}: "
                           f"{p.stderr.decode(errors='replace').strip()[:200]}")
        out = []
        for line in p.stdout.decode(errors="replace").splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2:
                out.append((parts[0], parts[1]))
        return out

    def require_device(self) -> str:
        """Pick a usable device or explain exactly why there is none."""
        devs = self.devices()
        if self.serial:
            state = dict(devs).get(self.serial)
            if state is None:
                raise AdbError(f"device {self.serial} is not connected "
                               f"(adb sees: {devs or 'nothing'})")
            if state != "device":
                raise AdbError(f"device {self.serial} is '{state}', not ready")
            return self.serial
        if not devs:
            raise AdbError(
                "no device/emulator connected. Start it with "
                "`emulator -avd PushObs -no-audio &` and wait for "
                "`adb shell getprop sys.boot_completed` to print 1."
            )
        ready = [s for s, st in devs if st == "device"]
        if not ready:
            states = ", ".join(f"{s}={st}" for s, st in devs)
            raise AdbError(f"no ready device; adb reports {states}. 'offline' usually "
                           f"means the emulator is still booting; 'unauthorized' means "
                           f"accept the USB-debugging prompt on screen.")
        self.serial = ready[0]
        return self.serial

    def shell(self, args: list[str], timeout: int = 45) -> str:
        p = self.run(["shell"] + args, timeout=timeout)
        if p.returncode != 0:
            err = p.stderr.decode(errors="replace").strip() or p.stdout.decode(errors="replace").strip()
            raise AdbError(f"`adb shell {' '.join(args)}` exit {p.returncode}: {err[:300]}")
        return p.stdout.decode(errors="replace")

    def exec_out(self, args: list[str], timeout: int = 45) -> bytes:
        p = self.run(["exec-out"] + args, timeout=timeout)
        if p.returncode != 0:
            err = (p.stderr.decode(errors="replace").strip()
                   or p.stdout.decode(errors="replace").strip())
            raise AdbError(f"`adb exec-out {' '.join(args)}` exit {p.returncode}: {err[:300]}")
        return p.stdout


# ---------------------------------------------------------------------------
# Device actions
# ---------------------------------------------------------------------------

def launch_app(adb: Adb, package: str) -> str:
    """Start the package's launcher activity. `monkey` needs no activity name."""
    out = adb.shell(["monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1"],
                    timeout=30)
    if "No activities found" in out or "monkey aborted" in out.lower():
        raise AdbError(f"{package} has no launcher activity -- is it installed? "
                       f"`adb shell pm list packages | grep {package}`")
    return out


def open_notification_settings(adb: Adb, package: str) -> str:
    return adb.shell([
        "am", "start", "-a", "android.settings.APP_NOTIFICATION_SETTINGS",
        "--es", "android.provider.extra.APP_PACKAGE", package,
    ], timeout=30)


def screencap(adb: Adb) -> bytes:
    data = adb.exec_out(["screencap", "-p"], timeout=60)
    if not data.startswith(b"\x89PNG"):
        raise AdbError(f"screencap returned {len(data)} bytes that are not a PNG "
                       f"(starts {data[:8]!r}); the screen may be off or the device mid-boot")
    return data


def ui_dump(adb: Adb) -> str:
    """Run uiautomator dump and read the file back. The dump command prints
    the path it wrote to; the content comes from a second read because the
    dump itself goes to the device filesystem, not stdout."""
    msg = adb.shell(["uiautomator", "dump", DEVICE_DUMP_PATH], timeout=60)
    if "ERROR" in msg.upper() and "dumped to" not in msg.lower():
        raise AdbError(f"uiautomator dump failed: {msg.strip()[:300]}")
    xml_text = adb.exec_out(["cat", DEVICE_DUMP_PATH], timeout=30).decode("utf-8", errors="replace")
    if "<hierarchy" not in xml_text:
        raise AdbError("uiautomator dump did not produce a hierarchy (got "
                       f"{xml_text.strip()[:120]!r}). If the screen shows a secure "
                       "window (keyboard, some login forms) the dump is empty by design.")
    return xml_text


def installed_packages(adb: Adb) -> list[str]:
    out = adb.shell(["pm", "list", "packages", "-3"], timeout=30)
    return sorted(line.split(":", 1)[1].strip() for line in out.splitlines()
                  if line.startswith("package:"))


# ---------------------------------------------------------------------------
# uiautomator XML -> readable UI state
# ---------------------------------------------------------------------------

TOGGLE_CLASSES = ("Switch", "CheckBox", "RadioButton", "ToggleButton", "CompoundButton",
                  "SwitchCompat", "MaterialSwitch", "CheckedTextView")
BUTTON_CLASSES = ("Button", "ImageButton")


@dataclass
class UiNode:
    index: int
    cls: str
    text: str
    desc: str
    resource_id: str
    checkable: bool
    checked: bool
    clickable: bool
    enabled: bool
    bounds: str
    depth: int
    parent: int = -1


def _b(v: str | None) -> bool:
    return (v or "").strip().lower() == "true"


def parse_ui_xml(xml_text: str) -> list[UiNode]:
    """Flatten a uiautomator hierarchy into document order. Raises AdbError on
    XML that is not a hierarchy at all, so a truncated dump is not mistaken
    for an empty screen."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise AdbError(f"uiautomator XML is malformed: {e}") from e
    nodes: list[UiNode] = []

    def walk(el, depth, parent):
        me = parent
        if el.tag == "node":
            me = len(nodes)
            nodes.append(UiNode(
                index=len(nodes),
                cls=(el.get("class") or "").rsplit(".", 1)[-1],
                text=(el.get("text") or "").strip(),
                desc=(el.get("content-desc") or "").strip(),
                resource_id=el.get("resource-id") or "",
                checkable=_b(el.get("checkable")),
                checked=_b(el.get("checked")),
                clickable=_b(el.get("clickable")),
                enabled=_b(el.get("enabled")) if el.get("enabled") is not None else True,
                bounds=el.get("bounds") or "",
                depth=depth,
                parent=parent,
            ))
        for child in el:
            walk(child, depth + 1, me)

    walk(root, 0, -1)
    return nodes


def _is_toggle(n: UiNode) -> bool:
    return n.checkable or n.cls in TOGGLE_CLASSES


def _label_for(nodes: list[UiNode], i: int) -> str:
    """A toggle's own text, else the FIRST text inside the same row (a
    settings row is title, optional summary, switch -- the title comes
    first), else the nearest preceding text in document order. The resource
    id is kept alongside so a wrong guess is checkable against the XML."""
    n = nodes[i]
    if n.text:
        return n.text
    if n.desc:
        return n.desc
    if n.parent >= 0:
        for j in range(n.parent + 1, i):
            p = nodes[j]
            if _is_toggle(p):
                continue
            if p.text or p.desc:
                return p.text or p.desc
    for j in range(i - 1, max(-1, i - 12), -1):
        p = nodes[j]
        if _is_toggle(p):
            break
        if p.text:
            return p.text
        if p.desc:
            return p.desc
    return n.resource_id.rsplit("/", 1)[-1] or "(unlabelled)"


def summarize_ui(nodes: list[UiNode]) -> dict:
    """What a person would write down looking at the screen."""
    texts: list[str] = []
    seen: set[str] = set()
    toggles: list[dict] = []
    buttons: list[dict] = []
    editable: list[str] = []
    for i, n in enumerate(nodes):
        if _is_toggle(n):
            toggles.append({
                "label": _label_for(nodes, i),
                "checked": n.checked,
                "enabled": n.enabled,
                "class": n.cls,
                "resource_id": n.resource_id,
            })
            continue
        if n.cls in BUTTON_CLASSES or (n.clickable and n.text):
            label = n.text or n.desc
            if label:
                buttons.append({"label": label, "resource_id": n.resource_id, "enabled": n.enabled})
        if n.cls == "EditText":
            editable.append(n.text or n.desc or n.resource_id)
        for t in (n.text, n.desc):
            if t and t not in seen:
                seen.add(t)
                texts.append(t)
    pkgs = sorted({n.resource_id.split(":", 1)[0] for n in nodes if ":" in n.resource_id})
    return {
        "texts": texts,
        "toggles": toggles,
        "buttons": buttons,
        "editable": editable,
        "packages_on_screen": pkgs,
        "node_count": len(nodes),
    }


# ---------------------------------------------------------------------------
# Notification channels (dumpsys notification)
# ---------------------------------------------------------------------------

IMPORTANCE = {
    -1000: "UNSPECIFIED", 0: "NONE (blocked)", 1: "MIN", 2: "LOW",
    3: "DEFAULT", 4: "HIGH", 5: "MAX",
}
_CHANNEL_RE = re.compile(r"NotificationChannel\{(.*)\}\s*$")
_FIELD_RE = re.compile(r"(m[A-Z]\w*)=('(?:[^'\\]|\\.)*'|[^,}]*)")
_PKG_LINE_RE = re.compile(r"^(\s*)([A-Za-z][\w.]*)\s+\((\d+)\)\s*(.*)$")


def parse_channel_line(line: str) -> dict | None:
    m = _CHANNEL_RE.search(line)
    if not m:
        return None
    fields: dict[str, str] = {}
    for k, v in _FIELD_RE.findall(m.group(1)):
        v = v.strip()
        if len(v) >= 2 and v[0] == "'" and v[-1] == "'":
            v = v[1:-1]
        fields[k] = v
    if "mId" not in fields:
        return None
    try:
        imp = int(fields.get("mImportance", "-1000"))
    except ValueError:
        imp = -1000
    return {
        "id": fields.get("mId", ""),
        "name": fields.get("mName", ""),
        "description": fields.get("mDescription", "") if fields.get("mDescription") != "null" else "",
        "importance": imp,
        "importance_label": IMPORTANCE.get(imp, str(imp)),
        "blocked": imp == 0,
        "group": fields.get("mGroup", "") if fields.get("mGroup") != "null" else "",
        "deleted": fields.get("mDeleted", "false") == "true",
        "bypass_dnd": fields.get("mBypassDnd", "false") == "true",
        "show_badge": fields.get("mShowBadge", "false") == "true",
        "user_locked": fields.get("mUserLockedFields", "0"),
    }


def parse_channels(dump_text: str, package: str) -> dict:
    """Channels for one package out of `dumpsys notification` output.

    On API 33-35 the per-package block in the preferences section looks like

        com.example.newsapp (10123) importance=DEFAULT showBadge=true ...
          NotificationChannel{mId='breaking', mName=Breaking News, ...}
          NotificationChannelGroup{...}

    The block ends at the next line with the same indentation that names a
    different package. If no block is found, the whole dump is scanned for
    channel lines as a fallback and `scoped` is False so the caller knows the
    result may include other packages' channels. `cmd notification` on these
    API levels has no list_channels subcommand (checked against the AOSP
    android14 NotificationShellCmd), so dumpsys is the only source.
    """
    lines = dump_text.splitlines()
    app_line = None
    block: list[str] = []
    indent = None
    for ln in lines:
        m = _PKG_LINE_RE.match(ln)
        if m and indent is None and m.group(2) == package:
            indent = len(m.group(1))
            app_line = ln.strip()
            continue
        if indent is not None:
            m2 = _PKG_LINE_RE.match(ln)
            if m2 and len(m2.group(1)) <= indent:
                break
            if ln.strip() and (len(ln) - len(ln.lstrip())) <= indent and not ln.lstrip().startswith("Notification"):
                break
            block.append(ln)

    scoped = indent is not None
    source = block if scoped else lines
    channels = [c for c in (parse_channel_line(ln) for ln in source) if c]
    app_importance = None
    permission = None
    if app_line:
        mi = re.search(r"importance=(\w+)", app_line)
        app_importance = mi.group(1) if mi else None
        permission = "blocked" if app_importance == "NONE" else "allowed" if app_importance else None
    return {
        "package": package,
        "scoped": scoped,
        "app_line": app_line,
        "app_importance": app_importance,
        "permission": permission,
        "channels": channels,
        "blocked": [c["id"] for c in channels if c["blocked"]],
    }


def fetch_channels(adb: Adb, package: str) -> dict:
    """dumpsys is the source of truth; --noredact is asked for and tolerated
    if absent. The raw text is returned too so it can be archived."""
    raw = None
    try:
        raw = adb.shell(["dumpsys", "notification", "--noredact"], timeout=60)
    except AdbError:
        raw = adb.shell(["dumpsys", "notification"], timeout=60)
    result = parse_channels(raw, package)
    result["source"] = "dumpsys notification"
    result["raw_chars"] = len(raw)
    # Newer builds may grow a shell subcommand; try it, ignore its absence.
    try:
        extra = adb.shell(["cmd", "notification", "list_channels", package], timeout=20)
        if extra.strip() and "unknown command" not in extra.lower():
            result["cmd_list_channels"] = extra.strip()[:4000]
    except AdbError:
        pass
    return result, raw


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slug(label: str) -> str:
    return _SLUG_RE.sub("-", label.lower()).strip("-")[:48] or "step"


@dataclass
class Step:
    number: int
    label: str
    ts: str
    png: str | None = None
    xml: str | None = None
    ok: bool = False
    error: str | None = None
    session: str = ""
    summary: dict = field(default_factory=dict)


class Session:
    def __init__(self, package: str, label: str = "", out_root: str = DEFAULT_OUT,
                 date: str | None = None):
        self.package = package
        self.label = label
        self.date = date or datetime.now().astimezone().strftime("%Y-%m-%d")
        self.dir = os.path.join(out_root, package, self.date)
        os.makedirs(self.dir, exist_ok=True)
        self.manifest_path = os.path.join(self.dir, "steps.json")
        self.manifest = self._load()

    def _load(self) -> dict:
        if os.path.isfile(self.manifest_path):
            try:
                with open(self.manifest_path, encoding="utf-8") as fh:
                    return json.load(fh)
            except (OSError, ValueError):
                pass
        return {"package": self.package, "date": self.date, "sessions": [], "steps": [],
                "channels": None}

    def _save(self) -> None:
        if self.label and self.label not in self.manifest["sessions"]:
            self.manifest["sessions"].append(self.label)
        with open(self.manifest_path, "w", encoding="utf-8") as fh:
            json.dump(self.manifest, fh, indent=2, ensure_ascii=False)
        with open(os.path.join(self.dir, "index.md"), "w", encoding="utf-8") as fh:
            fh.write(render_index(self.manifest))

    def next_number(self) -> int:
        return 1 + max((s["number"] for s in self.manifest["steps"]), default=0)

    def capture(self, adb: Adb, label: str) -> Step:
        """One step: PNG + XML + summary. A failure is recorded, not hidden."""
        n = self.next_number()
        base = f"{n:02d}-{slug(label)}"
        step = Step(number=n, label=label, ts=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    session=self.label)
        try:
            png = screencap(adb)
            with open(os.path.join(self.dir, base + ".png"), "wb") as fh:
                fh.write(png)
            step.png = base + ".png"
            xml_text = ui_dump(adb)
            with open(os.path.join(self.dir, base + ".xml"), "w", encoding="utf-8") as fh:
                fh.write(xml_text)
            step.xml = base + ".xml"
            step.summary = summarize_ui(parse_ui_xml(xml_text))
            step.ok = True
        except AdbError as e:
            step.error = str(e)
        self.manifest["steps"].append(asdict(step))
        self._save()
        return step

    def record_channels(self, result: dict) -> None:
        self.manifest["channels"] = result
        with open(os.path.join(self.dir, "channels.json"), "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2, ensure_ascii=False)
        self._save()

    def rebuild_index(self) -> str:
        """Re-derive summaries from the XML files on disk, then rewrite index.md."""
        for s in self.manifest["steps"]:
            if s.get("xml"):
                p = os.path.join(self.dir, s["xml"])
                if os.path.isfile(p):
                    try:
                        with open(p, encoding="utf-8") as fh:
                            s["summary"] = summarize_ui(parse_ui_xml(fh.read()))
                        s["ok"] = True
                        s["error"] = None
                    except AdbError as e:
                        s["ok"] = False
                        s["error"] = str(e)
        self._save()
        return os.path.join(self.dir, "index.md")


def render_step_md(s: dict) -> str:
    L = [f"## {s['number']:02d} — {s['label']}", ""]
    L.append(f"_{s['ts']}_" + (f" · session: {s['session']}" if s.get("session") else ""))
    L.append("")
    if not s.get("ok"):
        L.append(f"**CAPTURE FAILED:** {s.get('error') or 'unknown error'}")
        L.append("")
        return "\n".join(L)
    if s.get("png"):
        L.append(f"![{s['label']}]({s['png']})")
        L.append("")
    sm = s.get("summary") or {}
    if sm.get("toggles"):
        L.append("**Toggles**")
        L.append("")
        L.append("| State | Label | Widget | id |")
        L.append("|---|---|---|---|")
        for t in sm["toggles"]:
            state = "ON" if t["checked"] else "off"
            if not t.get("enabled", True):
                state += " (disabled)"
            L.append(f"| {state} | {t['label']} | {t['class']} | `{t['resource_id'].rsplit('/', 1)[-1]}` |")
        L.append("")
    if sm.get("buttons"):
        L.append("**Buttons:** " + " · ".join(f"[{b['label']}]" for b in sm["buttons"]))
        L.append("")
    if sm.get("editable"):
        L.append("**Inputs:** " + ", ".join(sm["editable"]))
        L.append("")
    if sm.get("texts"):
        L.append("**Visible text**")
        L.append("")
        for t in sm["texts"]:
            L.append(f"- {t}")
        L.append("")
    if sm.get("packages_on_screen"):
        L.append(f"_Packages drawing on screen: {', '.join(sm['packages_on_screen'])}_")
        L.append("")
    return "\n".join(L)


def render_channels_md(c: dict | None) -> str:
    if not c:
        return ""
    L = ["## OS notification channels", ""]
    L.append(f"Source: `{c.get('source', 'dumpsys notification')}`" +
             ("" if c.get("scoped") else " — **package block not found; list is unscoped and may "
                                        "include other apps' channels**"))
    if c.get("app_line"):
        L.append("")
        L.append(f"App-level: `{c['app_line']}`  → permission **{c.get('permission') or 'unknown'}**")
    L.append("")
    if not c.get("channels"):
        L.append("No channels registered. Either the app has not yet created any (it does so "
                 "lazily, usually on first launch or first alert) or notifications were never granted.")
        L.append("")
        return "\n".join(L)
    L.append("| Channel | id | Importance | Group | Bypass DND |")
    L.append("|---|---|---|---|---|")
    for ch in c["channels"]:
        L.append(f"| {ch['name']} | `{ch['id']}` | {ch['importance_label']} | "
                 f"{ch['group'] or '—'} | {'yes' if ch['bypass_dnd'] else 'no'} |")
    L.append("")
    if c.get("cmd_list_channels"):
        L.append("`cmd notification list_channels` also answered:")
        L.append("")
        L.append("```")
        L.append(c["cmd_list_channels"])
        L.append("```")
        L.append("")
    return "\n".join(L)


def render_index(m: dict) -> str:
    L = [f"# Observation log — `{m['package']}` — {m['date']}", ""]
    if m.get("sessions"):
        L.append("Sessions: " + "; ".join(m["sessions"]))
        L.append("")
    steps = m.get("steps", [])
    ok = sum(1 for s in steps if s.get("ok"))
    L.append(f"{len(steps)} steps, {ok} captured, {len(steps) - ok} failed. "
             f"Generated by `observe.py`; screenshots and uiautomator XML sit beside this file.")
    L.append("")
    if steps:
        L.append("| # | Step | Toggles on/total | Captured |")
        L.append("|---|---|---|---|")
        for s in steps:
            tg = (s.get("summary") or {}).get("toggles", [])
            on = sum(1 for t in tg if t["checked"])
            L.append(f"| {s['number']:02d} | [{s['label']}](#{s['number']:02d}--{slug(s['label'])}) | "
                     f"{on}/{len(tg)} | {'yes' if s.get('ok') else 'FAILED'} |")
        L.append("")
    for s in steps:
        L.append(render_step_md(s))
    L.append(render_channels_md(m.get("channels")))
    return "\n".join(L).rstrip() + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _interactive(session: Session, adb: Adb) -> int:
    print("Interactive capture. Type a step label and press Enter to capture; "
          "blank line or 'q' to finish.")
    print("Protocol labels: " + ", ".join(k for k, _ in PROTOCOL_STEPS))
    n = 0
    while True:
        try:
            label = input(f"[{session.next_number():02d}] step label> ").strip()
        except EOFError:
            break
        if not label or label.lower() in ("q", "quit", "exit"):
            break
        st = session.capture(adb, label)
        _report(st)
        n += 1
    print(f"{n} steps captured → {session.dir}/index.md")
    return 0


def _report(st: Step) -> None:
    if st.ok:
        tg = st.summary.get("toggles", [])
        print(f"  {st.number:02d} {st.label}: {st.png}, {len(st.summary.get('texts', []))} texts, "
              f"{len(tg)} toggles ({sum(1 for t in tg if t['checked'])} on)")
        for t in tg:
            print(f"       [{'x' if t['checked'] else ' '}] {t['label']}")
    else:
        print(f"  {st.number:02d} {st.label}: FAILED — {st.error}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Screenshot + UI dump an app step by step over adb")
    ap.add_argument("--package", "-p", default=None, help="Android package id, e.g. com.example.newsapp (required for device actions)")
    ap.add_argument("--session", "-s", default="", help="a label for this sitting, e.g. 'first install'")
    ap.add_argument("--step", action="append", default=[], metavar="LABEL",
                    help="capture one step (repeatable)")
    ap.add_argument("--interactive", "-i", action="store_true", help="prompt for step labels")
    ap.add_argument("--launch", action="store_true", help="launch the app first")
    ap.add_argument("--notification-settings", action="store_true",
                    help="open the OS notification settings for the package, capture, and dump channels")
    ap.add_argument("--channels-only", action="store_true",
                    help="dump channels without opening the settings screen")
    ap.add_argument("--index-only", action="store_true", help="rebuild index.md from files on disk")
    ap.add_argument("--protocol", action="store_true", help="print the protocol step labels and exit")
    ap.add_argument("--out", default=DEFAULT_OUT, help="observations root (default ./observations)")
    ap.add_argument("--date", help="override the folder date (YYYY-MM-DD)")
    ap.add_argument("--adb", help="explicit adb path")
    ap.add_argument("--serial", help="device serial")
    ap.add_argument("--wait", type=float, default=1.5,
                    help="seconds to wait after --launch / opening settings before capturing")
    args = ap.parse_args(argv)
    if not getattr(args, 'protocol', False) and getattr(args, 'package', None) is None:
        print('observe.py: nothing to do — pass --package <android package id> with an action, or --protocol', file=sys.stderr)
        return 2

    if args.protocol:
        print("Step labels used by projects/output/abc-news-app-observation.md:\n")
        for k, why in PROTOCOL_STEPS:
            print(f"  {k:<26} {why}")
        return 0

    session = Session(args.package, args.session, args.out, args.date)

    if args.index_only:
        path = session.rebuild_index()
        print(f"rebuilt {path} ({len(session.manifest['steps'])} steps)")
        return 0

    wants_device = bool(args.step or args.interactive or args.launch
                        or args.notification_settings or args.channels_only)
    if not wants_device:
        ap.print_usage()
        print("nothing to do: pass --step LABEL, --interactive, --notification-settings, "
              "--index-only or --protocol", file=sys.stderr)
        return 2

    adb = Adb(find_adb(args.adb), args.serial)
    try:
        serial = adb.require_device()
    except AdbError as e:
        print(f"observe.py: {e}", file=sys.stderr)
        return 1
    print(f"device {serial} · package {args.package} · folder {session.dir}")

    import time
    try:
        if args.launch:
            launch_app(adb, args.package)
            time.sleep(args.wait)
        for label in args.step:
            _report(session.capture(adb, label))
        if args.interactive:
            _interactive(session, adb)
        if args.notification_settings or args.channels_only:
            if args.notification_settings:
                open_notification_settings(adb, args.package)
                time.sleep(args.wait)
                _report(session.capture(adb, "os-notification-settings"))
            result, raw = fetch_channels(adb, args.package)
            with open(os.path.join(session.dir, "dumpsys-notification.txt"), "w", encoding="utf-8") as fh:
                fh.write(raw)
            session.record_channels(result)
            chans = result["channels"]
            print(f"  channels: {len(chans)}" + ("" if result["scoped"] else " (UNSCOPED — package block not found)"))
            for ch in chans:
                print(f"       {ch['importance_label']:<15} {ch['name']}  [{ch['id']}]")
    except AdbError as e:
        print(f"observe.py: {e}", file=sys.stderr)
        session._save()
        return 1
    print(f"index: {os.path.join(session.dir, 'index.md')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
