# launchd jobs

Three templates. `install.sh` substitutes the paths and loads them.

| Job | What | When |
|---|---|---|
| `com.pushobs.capture` | the 30s polling loop | always, restarted if it dies |
| `com.pushobs.recap` | cluster → enrich → recap | 21:00 local, daily |
| `com.pushobs.emulator` | keeps the AVD running | always, **install last** |

## Install

```bash
./launchd/install.sh                    # capture + recap
./launchd/install.sh --avd PushObs      # + the emulator job
./launchd/install.sh --uninstall        # remove all three
```

The script refuses to install if `.venv/bin/python` is missing or if any
`__PLACEHOLDER__` survives substitution, and it runs `plutil -lint` on each
generated plist before loading it.

## Why the emulator job goes last

Install it only after you have booted the AVD by hand, completed Google
sign-in, and installed the apps. A headless relaunch of an AVD that has never
finished first-run setup sits on a setup screen receiving no pushes — and it
looks healthy from the outside, which is the worst kind of failure here.

`-no-snapshot-load` is deliberate for the same reason: restoring a snapshot can
restore a stale network state in which FCM never reconnects.

## Checking on them

```bash
launchctl list | grep pushobs
tail -f logs/capture.log
tail -f logs/recap.log
```

A launchd job gets a minimal PATH, which is why the capture plist sets
`PATH` and `ANDROID_SDK_ROOT` explicitly — without that, `adb` is not found.

## Manual control

```bash
launchctl kickstart -k gui/$UID/com.pushobs.recap    # run the recap now
launchctl bootout gui/$UID/com.pushobs.capture       # stop capture
```
