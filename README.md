# The Push Observatory

This tool captures and analyzes push notifications from Android news apps to
study timing, volume, and copy patterns across outlets. One Android device,
subscribed to as many news apps as you configure, feeds one dashboard and a
nightly AI recap.

The dashboard is a weekend's work. **The data is not** — it accrues in real
time and cannot be backfilled. Everything in this repo is arranged around one
priority: get alerts landing in `alerts.db` as early as possible, even if
nothing is analysed yet.

### Example data

`packages.py` ships pre-populated with the public app package IDs and RSS
feeds of ~18 US news outlets (including a US broadcast network and its
national app) as demo/example configuration, and `seed_demo.py` generates
synthetic alerts in that same outlet mix so the dashboard is browsable before
any real capture exists. Swap in your own outlet list to point the tool at a
different set of apps — nothing else in the pipeline assumes any particular
publisher.

## Live demo

**[https://push-observatory-demo.vercel.app](https://push-observatory-demo.vercel.app)** — a static, read-only build of the dashboard against
a richer 30-day synthetic dataset, deployed on Vercel.

This is a portfolio piece, not a ranking of any outlet: `FOCUS_OUTLET` is
unset in the demo, so the focus-outlet-vs-field card stays disabled and no
single publisher is singled out. The demo shows what the tool does and what
production would add — see the "About this tool" drawer and the "Demo
dataset" pill in the top bar, which explains exactly what is synthetic.

**What's synthetic in the demo:** every alert (30 days × 18 outlets, generated
by `demo/build_dataset.py` with a fixed seed — realistic daily rhythm,
per-outlet style, and cross-outlet story clusters, but no headline is a claim
about a real event), the capture log and package snapshot (a sample of what a
healthy instrument looks like), and the Observations notes (each tagged
`[SAMPLE DEMO NOTE]`). The Nightly recap tab shows the same no-LLM statistical
digest `recap.py --no-llm` produces — no API key needed for the demo.

**Building the site yourself:**

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python build_site.py          # writes ./site
python3 -m http.server -d site 8000     # serve it locally to check
```

`build_site.py` seeds a throwaway database with `demo/build_dataset.py`, then
drives every GET endpoint the dashboard calls through FastAPI's `TestClient`
and writes each response as a JSON file under `site/data/`. It copies
`dashboard/static/index.html` to `site/index.html` with a small shim
(`window.STATIC_BASE = "./data"`) so the same front-end code fetches those
files instead of a live API; write actions (adding/deleting an observation
note) are disabled in that mode. `site/vercel.json` sends
`X-Robots-Tag: noindex, nofollow, noarchive` on every path, so the demo is
never indexed. Deploy `site/` to Vercel as a static site (no build step).

---

## STATUS — read this first

| Piece | State |
|---|---|
| Parser, capture loop, clustering, copy analysis, dashboard, recap, seed data | **Built and tested** |
| `observe.py` (app observation), Monitoring + Observations tabs, focus-outlet-vs-field card | **Built and tested against fixtures and mocked adb.** Never run against a real device yet |
| Android emulator (AVD), Google sign-in, the ~18 news apps | **Not done — requires a human.** See [RUNBOOK](#runbook) |
| Android SDK on this machine | **Not installed.** See [Step 1](#human-step-1--install-the-android-sdk) |
| Real captured alerts | **Zero.** The dashboard currently shows synthetic demo data, clearly marked by a "Demo dataset" pill |
| `com.example.pushlistener` APK | Source complete, **never compiled** (no SDK on the build machine) |

Nothing in this repo pretends the manual steps are done. The dashboard shows
a "Demo dataset" pill in the top bar whenever synthetic rows are present
(click it for exactly what's synthetic), and `seed_demo.py --purge` removes
them.

---

## Architecture

```
Android emulator (AVD, Google Play system image)
  ├─ ~18 news apps, all alert categories enabled
  └─ notifications land in the shade
        │
        │  path A (start here):  adb shell dumpsys notification --noredact
        │                        polled every 30s
        │  path B (upgrade):     NotificationListenerService APK
        │                        → POST http://10.0.2.2:8787/ingest
        ▼
   capture.py / collector.py
      parse (package, title, body, postTime) → dedupe on (pkg, title, postTime)
        │
        ▼
   alerts.db (SQLite)
        │
        ├─→ enrich.py   RSS/URL match → canonical story
        ├─→ cluster.py  same-event grouping → first-mover lag
        │
        ├─→ dashboard/  FastAPI + single page: timeline, race, volume, copy,
        │                 + Monitoring (instrument health) + Observations (qualitative log)
        └─→ recap.py    9pm → MiniMax → ntfy.sh → phone

   observe.py  ── adb screencap + uiautomator dump per step ──▶ observations/<pkg>/<date>/
                  (what the app SHOWS, next to what it SENDS)      PNG + XML + index.md
```

### Why Android and not iOS

App Store apps cannot run in the iOS Simulator, and the Simulator cannot
register with real APNs. There is no way to receive a live push from CNN's
actual app on iOS without a physical device. Android emulators run Google Play
system images, so real apps sign in and receive real FCM pushes.

### The files

| File | What it does |
|---|---|
| `parser.py` | Parses `dumpsys notification` output. The load-bearing piece — unit tested against fixtures |
| `capture.py` | The 30s polling loop. Never exits on error; `--once` and `--replay` modes for testing |
| `db.py` | SQLite schema, dedupe hash, connection handling |
| `packages.py` | Android package → outlet mapping, **with a confidence level on every entry** |
| `enrich.py` | Matches alerts to canonical stories via RSS |
| `cluster.py` | Groups same-event alerts across outlets, computes first-mover lag |
| `copy_analysis.py` | Length, restatement vs reason-to-open, urgency, emoji, breaking vs promo |
| `seed_demo.py` | Small synthetic demo data (a week), every row flagged `synthetic=1` |
| `demo/build_dataset.py` | Full 30-day/18-outlet synthetic dataset used by the live demo, plus capture log, package snapshot, and sample observation notes |
| `build_site.py` | Static export of the dashboard (see [Live demo](#live-demo)) |
| `recap.py` | 9pm job → MiniMax (`MiniMax-M2.7` via the Anthropic-compatible endpoint) → ntfy.sh |
| `collector.py` | Host-side HTTP sink for the listener APK (path B) |
| `observe.py` | Step-by-step app observation over adb: screenshot + UI dump + extracted text/toggle states per step, OS channel dump. Unit tested against fixture XML and canned adb output |
| `monitoring.py` | Instrument health: capture liveness, adb/emulator state, per-outlet silence-vs-no-news, miss-rate proxy, installed-vs-expected packages (`--snapshot`) |
| `dashboard/` | FastAPI + one HTML page: four analysis views, Coverage, Monitoring, Observations |
| `launchd/` | plists + installer for capture, recap, emulator |
| `com.example.pushlistener/` | NotificationListenerService source (path B) |

Everything except the dashboard and the recap uses **only the standard
library**. That is deliberate: the capture loop must survive unattended for
weeks and should not be able to break because of a dependency upgrade.

---

# RUNBOOK

Two kinds of step. **HUMAN STEPS** need a GUI, a Google account, or a physical
toggle and cannot be scripted. **AUTOMATED STEPS** are commands.

Do them in this order.

---

## AUTOMATED STEP 0 — set up the Python side (2 min)

Nothing here needs the emulator. Do it now and confirm the code works.

```bash
cd projects/push-observatory

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Verify the parser against fixtures — no emulator needed
.venv/bin/python -m pytest tests/ -q          # expect: 142 passed

# Verify the capture path end to end against a saved dump
.venv/bin/python capture.py --replay tests/fixtures/dumpsys_notification_sample.txt --dry-run

# See the dashboard right now, with synthetic data
.venv/bin/python seed_demo.py --days 7
.venv/bin/python cluster.py
.venv/bin/python -m uvicorn dashboard.app:app --port 8000
# → http://127.0.0.1:8000
```

The "Demo dataset" pill at the top of the dashboard will tell you that
everything you are looking at is synthetic. When real data arrives, run
`python seed_demo.py --purge`.

---

## HUMAN STEP 1 — install the Android SDK

**Not currently installed on this machine.** No `~/Library/Android/sdk`, no
`adb`, no `emulator`, no Android Studio, no AVDs.

Two options. **Option A is recommended** — the AVD manager GUI is genuinely
easier for the Play Store image selection, and you will want the emulator
window anyway for app sign-in.

### Option A — Android Studio (GUI, recommended)

1. Download from <https://developer.android.com/studio> (Apple Silicon build).
2. Install and open it. On first launch it runs a setup wizard — accept the
   defaults; it installs the SDK to `~/Library/Android/sdk`.
3. Continue to **HUMAN STEP 2**.

### Option B — command-line tools only

```bash
brew install --cask android-commandlinetools
# then, accepting licences interactively:
sdkmanager --install "platform-tools" "emulator" \
           "system-images;android-34;google_apis_playstore;arm64-v8a"
```

These are large downloads (the system image alone is ~1.5 GB), which is why
nothing was installed automatically.

### Then add the tools to your PATH

```bash
cat >> ~/.zshrc <<'EOF'
export ANDROID_SDK_ROOT="$HOME/Library/Android/sdk"
export PATH="$ANDROID_SDK_ROOT/platform-tools:$ANDROID_SDK_ROOT/emulator:$ANDROID_SDK_ROOT/cmdline-tools/latest/bin:$PATH"
EOF
source ~/.zshrc
adb version && emulator -version
```

---

## AUTOMATED STEP 2 — create the AVD

Once `sdkmanager` and `avdmanager` exist, **AVD creation is scriptable** and
you should script it. Only the Google sign-in and app installs are not.

```bash
# The Play Store image is required. Plain AOSP images have no Play Services
# and therefore no FCM, so no real pushes will ever arrive.
sdkmanager --install "system-images;android-34;google_apis_playstore;arm64-v8a"

echo "no" | avdmanager create avd \
  --name PushObs \
  --package "system-images;android-34;google_apis_playstore;arm64-v8a" \
  --device "pixel_7"

# Give it room — this device will run for weeks with ~18 apps installed
cat >> ~/.android/avd/PushObs.avd/config.ini <<'EOF'
hw.ramSize=4096
disk.dataPartition.size=16G
hw.keyboard=yes
EOF

emulator -list-avds        # should print: PushObs
emulator -avd PushObs -no-audio -no-boot-anim &
adb wait-for-device
adb shell getprop sys.boot_completed     # "1" when ready
```

**Verify you got a Play Store image** — this is the single most common way to
waste a day:

```bash
adb shell pm list packages | grep -E 'com.android.vending|com.google.android.gms'
```

Both must be present. If they are not, you built an AOSP image; delete the AVD
and recreate it with a `google_apis_playstore` package.

---

## HUMAN STEP 3 — sign into Google

**GUI only. Cannot be scripted.**

In the emulator window:

1. Open the **Play Store** app.
2. Sign in with a **throwaway Google account** — not your personal one. You are
   about to give ~18 news apps a notification firehose into it.
   - Create one first at <https://accounts.google.com/signup> if needed.
   - If Google blocks sign-in on the emulator, complete a 2FA challenge on
     another device, or use an account with no 2FA.
3. Accept the terms and skip the payment method and backup prompts.

---

## HUMAN STEP 4 — install the apps

**GUI only.** Search each in the Play Store and install.

The `?` and `!` marks below are the confidence level of the package ID recorded
in `packages.py` — see [Confirming package names](#confirming-package-names-on-the-device).

| Outlet | Package | Notes |
|---|---|---|
| ABC News | `com.abc.abcnews` | |
| AP News | `mnn.Android` ? | odd package name; it is correct |
| BBC News | `bbc.mobile.news.ww` | the international build |
| Bloomberg | `com.bloomberg.android.plus` | subscription gated |
| CBS News | `com.cbsnews.ott` **!** | may be the TV build — see below |
| CNN | `com.cnn.mobile.android.phone` | |
| ESPN | `com.espn.score_center` | |
| Fox News | `com.foxnews.android` | |
| Los Angeles Times | `com.apptivateme.next.la` ? | the local-push question |
| NBC News | `com.zumobi.msnbc` | legacy package name; it is NBC News |
| NPR | `org.npr.android.news` | NPR One was merged into this app |
| Reuters | `com.thomsonreuters.reuters` | |
| The Athletic | `com.theathletic` | subscription gated |
| The Guardian | `com.guardian` | |
| The New York Times | `com.nytimes.android` | |
| The Wall Street Journal | `wsj.reader_sp` | subscription gated |
| The Washington Post | `com.washingtonpost.android` | |
| USA Today | `com.usatoday.android.news` | |
| ~~Axios~~ | — | **no Android app.** Axios sunset its apps in late 2024 |

Axios being absent is a finding, not a gap: an outlet that abandoned the push
channel entirely is a data point about channel strategy. It stays in
`packages.py` for that reason.

**Where an app demands a login or subscription to unlock alerts, write it
down.** That gate is itself a finding about which outlets treat push as an
owned-relationship product versus a reach channel. Note it in your own
findings file, or in the dashboard's Observations tab.

---

## HUMAN STEP 5 — turn on every alert category

**This is the step that decides whether the project produces anything.**

There are **two separate layers**, and enabling only the first is the most
common failure:

### Layer 1 — the OS permission

Android 13+ requires apps to ask. Grant it on first launch, or afterwards:

**Settings → Apps → *[app]* → Notifications → Allow notifications**

### Layer 2 — the app's own alert categories

Almost every news app ships with most categories **off**. This lives in the
app's own settings, not the OS. Typical paths:

| App | Where |
|---|---|
| ABC News | Profile/☰ → Settings → Notifications → enable Breaking News, Top Stories, and every topic |
| CNN | ☰ → Settings → Notifications → Breaking News + all topics |
| NYT | Account → Settings → Notifications → enable all (Breaking, Morning Briefing, sections) |
| Washington Post | ☰ → Settings → Notifications → News alerts + all topics |
| Fox News | ☰ → Settings → Notifications |
| NBC / CBS / ABC | Settings → Notifications / Alerts |
| AP News | ☰ → Settings → Notifications → all topic toggles |
| Reuters | Profile → Settings → Notifications |
| BBC | ☰ → Settings → Notifications → My News alerts |
| Guardian | ☰ → Settings → Notifications → Breaking + Following |
| ESPN / The Athletic | Settings → Notifications → **also follow teams**, or you get nothing |
| WSJ / Bloomberg | Settings → Notifications → all alerts |
| USA Today / LA Times | Settings → Notifications → Breaking + Local |
| NPR | Settings → Notifications |

**Sports apps need teams followed.** ESPN and The Athletic send almost nothing
until you follow teams. Follow a broad set — a few NFL, NBA, MLB and Premier
League teams — or those two lanes will stay empty and you will wrongly
conclude they do not push.

Then verify the OS layer with adb, which *is* scriptable:

```bash
for p in com.cnn.mobile.android.phone com.nytimes.android com.abc.abcnews; do
  echo -n "$p: "
  adb shell dumpsys notification | grep -A2 "NotificationChannel.*$p" | head -3
done

# Or check the permission directly
adb shell cmd appops get <package> POST_NOTIFICATION
```

`adb` can read this state but **cannot flip the in-app category toggles** —
those live in each app's own preferences, behind its own UI.

---

## AUTOMATED STEP 6 — start capturing

```bash
cd projects/push-observatory

# Confirm adb sees the device and dumpsys returns something sane
adb devices
adb shell dumpsys notification --noredact | head -40

# One capture cycle, printing what it found
.venv/bin/python capture.py --once -v

# Archive raw dumps too — do this for the first day at least, so the
# parser can be validated against real output (see below)
.venv/bin/python capture.py --interval 30 --save-raw raw/
```

Leave it running. Then install it under launchd so it survives reboots:

```bash
./launchd/install.sh                       # capture + 9pm recap
./launchd/install.sh --avd PushObs         # + keep the emulator up

tail -f logs/capture.log
launchctl list | grep pushobs
```

Install the emulator job **only after** the AVD has completed first-run setup
by hand — a headless relaunch of an unconfigured AVD sits at a setup screen
receiving nothing.

### Confirming package names on the device

Three of the package IDs in `packages.py` are marked below `verified`. Check
them against reality once the apps are installed:

```bash
adb shell pm list packages -3 | sed 's/^package://' | sort
.venv/bin/python packages.py           # prints the expected list
```

If an outlet appears in the device list under a different ID, fix
`packages.py`. `capture.py` logs every unmapped package it sees and the
dashboard's **Coverage** tab shows outlets with zero alerts, so a wrong ID
surfaces rather than silently dropping an outlet.

`com.cbsnews.ott` is the one to watch — CBS ships both a phone app and a TV
app, and the OTT build will not push mobile alerts.

### Validating the parser against real output

The fixtures in `tests/fixtures/` were reconstructed from the AOSP dump format,
**not copied from a live device** — there was no Android SDK on the build
machine. The parser is written to tolerate format variation, but confirm it on
day one:

```bash
adb shell dumpsys notification --noredact > raw/first-real-dump.txt
.venv/bin/python capture.py --replay raw/first-real-dump.txt --dry-run -v
```

If the parsed count looks wrong versus what you can see in the notification
shade, diff `raw/first-real-dump.txt` against
`tests/fixtures/dumpsys_notification_sample.txt`, adjust `parser.py`, and add
the real dump as a new fixture. **Redact nothing on the way in** — but note
that `raw/` is gitignored, because the brief is explicit: publish the analysis,
not the corpus.

---

## AUTOMATED STEP 6b — observe an app's first-install behaviour (the day it is installed)

**Do this before turning on every alert category.** The defaults are the
finding, and onboarding and the permission pre-prompt only happen once. Run
this once per outlet you care about (the example below uses one outlet's
package, since it ships as example config in `packages.py`, but any package
works):

```bash
cd projects/push-observatory
adb devices                                            # state must be "device"
.venv/bin/python monitoring.py --snapshot              # record installed packages

S='.venv/bin/python observe.py --package <package> --session "first install"'
$S --step install                                      # Play Store page open
$S --launch --step first-launch                        # cold start
$S --interactive                                       # type a label per screen, e.g.:
                                                       #   onboarding-1, notif-preprompt, notif-os-prompt,
                                                       #   home-feed, home-feed-end, settings-path-1,
                                                       #   alert-settings, alert-detail, follow-topic,
                                                       #   follow-topic-after, location, account-prompt
$S --notification-settings                             # OS channel screen + dumpsys channel list
open observations/<package>/$(date +%F)/index.md
```

Each step writes `NN-label.png` + `NN-label.xml` and regenerates `index.md`
with every visible text and every toggle's on/off state. `--notification-settings`
opens *Settings → Apps → [app] → Notifications*, captures it, and parses the
app's registered channels out of `dumpsys notification` into `channels.json`
(there is no `cmd notification list_channels` on API 34/35; the tool tries it
anyway and ignores its absence). A failed capture is recorded as failed in
`index.md`, never papered over.

`observe.py --protocol` prints the step labels; `observe.py --index-only`
rebuilds `index.md` from the files on disk. Same tool, any package:
`--package com.nytimes.android`.

Then open the dashboard's **Observations** tab: the screenshots and toggle
states render inline, and you can add dated notes per outlet while it is
fresh -- this is where a cross-outlet comparison of onboarding and alert
defaults accumulates.

`observations/` is gitignored (it contains screenshots of third-party apps);
only `.gitkeep` is committed.

---

## AUTOMATED STEP 7 — analyse

```bash
.venv/bin/python cluster.py                 # group events, compute lag
.venv/bin/python enrich.py                  # match to canonical stories via RSS
.venv/bin/python -m uvicorn dashboard.app:app --port 8000
```

Four views:

1. **Timeline** — swimlane, one row per outlet, x = time of day. The shape of a
   news day at a glance.
2. **Who went first** — the money view. First-mover leaderboard plus every
   clustered story with per-outlet lag in minutes.
3. **Volume & cadence** — alerts/day/outlet, send-time histograms, overnight
   behaviour, weekday vs weekend.
4. **Copy analysis** — length, restatement vs reason-to-open, urgency markers,
   emoji, breaking vs promotional mix.

Plus:

- A **focus outlet vs. field** card at the top of the Timeline view — first-mover
  rate, volume vs median, breaking share, reason-to-open rate — computed by
  `recap.gather_window` / `recap.focus_summary`, the same functions the
  nightly recap uses, so the card and the 9pm message can never disagree. Set
  `FOCUS_OUTLET` in `.env` to enable it; leave it blank and the card shows a
  neutral "set FOCUS_OUTLET to compare one outlet against the field" message
  while every other view keeps working normally.
- **Coverage** — which outlets are producing alerts, which package IDs are
  unverified.
- **Monitoring** — is the *instrument* working: capture-loop liveness (from
  `capture_log`), adb/emulator state, per-outlet hours-since-last-alert with
  an explicit verdict — `receiving` / `quiet` (news lull) / `silent` (suspect
  the app) / `unknown` (the capture loop is not live, so the silence is ours)
  / `never` — a parser miss-rate proxy (share of real alerts first seen in
  the dump's *historical* section, i.e. missed live), and installed-vs-
  expected packages from the latest `monitoring.py --snapshot`. Synthetic rows
  never count as "seen".
- **Observations** — the qualitative log: `observe.py` captures rendered
  inline with toggle states and OS channels, plus dated notes stored in the
  `observations` table (`POST /api/observations`, no auth; it is a local tool).

`python monitoring.py` prints the same health report in the terminal.

---

## HUMAN STEP 8 — set up the nightly recap

```bash
cp .env.example .env
# Generate an unguessable ntfy topic — it is a PUBLIC URL
python3 -c "import secrets; print('pushobs-'+secrets.token_hex(8))"
```

Put that topic in `.env` as `PUSHOBS_NTFY_TOPIC`, and your `MINIMAX_API_KEY`.

**Human step:** install the **ntfy** app on your phone
([iOS](https://apps.apple.com/app/ntfy/id1625396347) /
[Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy)) and
subscribe to that exact topic.

Then test before trusting the 9pm job:

```bash
.venv/bin/python recap.py --dry-run --no-llm     # digest only, prints locally
.venv/bin/python recap.py --dry-run              # with MiniMax, still local
.venv/bin/python recap.py                        # delivers for real
```

The recap degrades rather than failing: no API key, no `anthropic` package, an
API error, or a refusal all produce a deterministic statistical digest that is
still delivered. A nightly job that dies silently is worse than a plain one.

---

## Optional — the robust capture path

If polling turns out to miss alerts, build the
[`com.example.pushlistener`](com.example.pushlistener/README.md) APK. Its README
explains how to measure the miss rate first, so you only spend the hour if the
data says to.

---

## What this is good for

Questions the dashboard is built to answer, for any outlet in the data:

- What is an outlet's **first-mover rate** on national breaking news vs the
  rest of the field? → *Who went first* tab.
- How many alerts/day does an outlet send vs the median? Under- or
  over-pushing? → *Volume* tab, which prints the comparison directly.
- Does an outlet's copy give a **reason to open**, or restate a headline? →
  *Copy* tab.
- Which outlets send **non-breaking** push — a personalised nudge, a "you
  haven't read today", a local hook? That is programmatic messaging in the
  wild, and it is the editorial-vs-lifecycle arbitration question with evidence
  behind it.
- Does anyone push **local**? Watch outlets with local editions.

Set `FOCUS_OUTLET` to spotlight one outlet against the field in the recap and
the dashboard's focus card; leave it unset to just browse the aggregate views.

---

## Notes on method and honesty

- **Synthetic data is quarantined.** Every seeded row carries `synthetic=1`;
  the dashboard flags it with the "Demo dataset" pill and can filter it out; `recap.py` excludes it by
  default. Purge with `seed_demo.py --purge`.
- **Package IDs carry confidence levels.** `packages.py` records
  verified/likely/unsure per outlet rather than presenting guesses as facts.
- **Enrichment is partial by construction.** Five outlets — AP, Reuters,
  Bloomberg, The Athletic, and ABC itself — have retired public RSS. "No
  canonical URL" never means "not a real story"; the dashboard reports match
  rate per outlet so this stays visible.
- **Clustering is a heuristic.** TF-IDF plus entity overlap inside a 90-minute
  window, no cluster spanning more than the window. Backend is pluggable
  (`--backend`); the embedding backend raises rather than silently falling back
  to TF-IDF, so a run configured for embeddings can never report TF-IDF numbers
  as embedding results.
- **Copy classification is a heuristic too.** Read the distributions, not the
  individual labels. The *Copy* tab includes a sample so you can spot-check the
  classifier, and `copy_analysis.py`'s `BOILERPLATE` / `HOOK_MARKERS` lists are
  the first thing to tune on real copy.
- Personal research on your own device with your own accounts. **Publish the
  analysis, not the corpus** — do not republish outlets' push copy wholesale.
  `raw/` and `alerts.db` are gitignored.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `adb not found` | Step 1 not done, or PATH not reloaded |
| `no device/emulator connected` | Emulator not running: `emulator -avd PushObs -no-audio &` |
| Dump parses but titles read `12 chars` | `--noredact` not taking effect. `capture.py` flags this loudly in its log |
| An outlet shows zero alerts | In order: app not installed → OS permission off → **in-app categories off** → wrong package ID |
| ESPN / The Athletic silent | You did not follow any teams |
| Everything silent | You built an AOSP image, not a Play Store one. Check for `com.android.vending` |
| Race view empty | `cluster.py` has not run, or fewer than 2 outlets have alerts |
| Monitoring shows every outlet `unknown` | The capture loop is not live. Fix that first; nothing else on the tab means anything until it is |
| `observe.py` says "not a PNG" | Screen is off or the device is mid-boot: `adb shell input keyevent KEYCODE_WAKEUP` |
| `observe.py` dump has no `<hierarchy>` | A secure window is on screen (keyboard, some login forms); the dump is empty by design |
| Channels list is "UNSCOPED" | The package's block was not found in `dumpsys notification` — the app has not registered channels yet, or the dump format differs; check `dumpsys-notification.txt` in the capture folder |
| Recap says "no LLM" | `MINIMAX_API_KEY` unset in `.env` — the digest still delivers |
