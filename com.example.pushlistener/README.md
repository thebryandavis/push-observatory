# PushListener — the robust capture path

A `NotificationListenerService` that receives **every** notification the system
posts and forwards it to `collector.py` on the host.

**This is the upgrade path, not the starting point.** Start with `capture.py`
polling. Build this when you have evidence you need it.

## Why it exists

`adb shell dumpsys notification` can only show notifications that are
*currently active*. An app that posts an alert and cancels or replaces it
inside the 30-second polling window is invisible to the polling path. News
apps do this routinely — a breaking alert gets replaced by an updated one
seconds later, or is cancelled when you open the story on another device.

`onNotificationPosted` fires synchronously on every post. There is no window
to fall through.

It also captures `onNotificationRemoved`, which the polling path cannot see at
all. Time-to-cancel is its own finding: an alert retracted after 40 seconds is
a newsroom correction you would otherwise never know happened.

## Measuring whether you actually need it

Before building this, get the miss rate from data you already have. Run the
polling capture at two cadences on consecutive days:

```bash
python capture.py --interval 30    # day 1
python capture.py --interval 10    # day 2
```

If the 10-second day captures materially more alerts per outlet than the
30-second day, notifications are being missed and this APK is worth the hour.
If the counts are close, polling is fine and you should spend the hour on
analysis instead.

## Build

Requires Android Studio (for the SDK and JDK 17) or a standalone Gradle +
Android SDK install.

```bash
cd com.example.pushlistener

# Point Gradle at your SDK
echo "sdk.dir=$HOME/Library/Android/sdk" > local.properties

# First build downloads the Gradle wrapper distribution
gradle wrapper --gradle-version 8.7        # only if ./gradlew is absent
./gradlew assembleDebug
```

The APK lands at `app/build/outputs/apk/debug/app-debug.apk`.

## Install and enable

```bash
# 1. Start the host collector FIRST, so nothing is spooled unnecessarily
python ../collector.py            # listens on 127.0.0.1:8787

# 2. Install
adb install -r app/build/outputs/apk/debug/app-debug.apk

# 3. Grant notification access — THIS STEP IS MANUAL AND CANNOT BE SCRIPTED
adb shell am start -a android.settings.ACTION_NOTIFICATION_LISTENER_SETTINGS
```

**Human step:** in the emulator, find **Push Observatory Listener** in the
list, toggle it on, and confirm the warning dialog. Android deliberately has
no programmatic path to grant notification access — a listener can read every
notification on the device, so the grant must be a physical user action.

There is an ADB shortcut that works on emulator/debug builds:

```bash
adb shell settings put secure enabled_notification_listeners \
  "com.example.pushlistener/com.example.pushlistener.PushListenerService"
adb shell cmd notification allow_listener \
  com.example.pushlistener/com.example.pushlistener.PushListenerService
```

Verify it took, rather than assuming:

```bash
adb shell settings get secure enabled_notification_listeners
```

If `com.example.pushlistener` is not in that colon-separated list, do it
through the UI.

## Verify it is working

```bash
# Watch the service's own log
adb logcat -s PushListener:*

# Fire a test notification from the device itself
adb shell cmd notification post -S bigtext -t 'Test' TestTag 'hello from adb'

# The collector should log it, and it should land in the DB
curl -s http://127.0.0.1:8787/healthz
```

## Networking note

The app posts to `http://10.0.2.2:8787/ingest`. `10.0.2.2` is the emulator's
alias for the host machine's loopback interface — it is not a real network
address and is not reachable from anywhere else. `collector.py` binds
`127.0.0.1` by default for the same reason.

Cleartext HTTP is permitted **only** for `10.0.2.2` and `localhost`, via
`res/xml/network_security_config.xml`. TLS enforcement stays on for every
other host.

If the host is unreachable (laptop asleep, collector not running), events are
appended to a local spool file inside the app's private storage and flushed in
order on the next successful post, so a sleeping laptop does not cost data.

## Running both paths at once

`collector.py` writes to the same `alerts` table with the same dedupe rule as
`capture.py`, so you can run both during a changeover without double-counting.
Rows from the listener carry `"source": "listener"` in their `raw` JSON, so you
can compare coverage afterwards:

```sql
SELECT json_extract(raw,'$.source') AS src, COUNT(*)
FROM alerts GROUP BY src;
```

## Not done here

The source compiles as written, but **it has not been built or run** — the
machine this was written on has no Android SDK. Expect to fix at least a
Gradle plugin version or an SDK path on first build. The service logic is the
part that matters and is straightforward; the build scaffolding is the part
most likely to need a nudge.
