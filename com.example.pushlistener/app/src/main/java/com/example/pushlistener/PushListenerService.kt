package com.example.pushlistener

import android.app.Notification
import android.content.Context
import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification
import android.util.Log
import org.json.JSONObject
import java.io.BufferedWriter
import java.io.File
import java.io.OutputStreamWriter
import java.net.HttpURLConnection
import java.net.URL
import java.util.concurrent.Executors

/**
 * Receives every notification the system posts and forwards it to the host.
 *
 * WHY THIS EXISTS
 * ---------------
 * The polling capture path (`adb shell dumpsys notification`) can only see
 * notifications that are *currently active*. An app that posts an alert and
 * cancels it inside the 30-second polling window is invisible. Several news
 * apps do exactly that -- they post a "breaking" alert and replace it with an
 * updated one seconds later, or cancel it when the user opens the app on
 * another device.
 *
 * `onNotificationPosted` fires on every post, synchronously, with no polling
 * window to fall through. That is the correct architecture. Polling is the
 * thing you build in an hour; this is the thing you build when you have
 * evidence that the miss rate matters.
 *
 * `onNotificationRemoved` is captured too, because time-to-cancel is itself
 * interesting -- an alert retracted after 40 seconds is a newsroom correction
 * you would otherwise never see.
 *
 * DELIVERY
 * --------
 * POSTs newline-delimited JSON to a collector on the host at 10.0.2.2 (the
 * emulator's alias for the host's loopback). If the host is unreachable, the
 * event is appended to a local spool file and retried on the next post, so a
 * laptop that went to sleep does not cost you data.
 */
class PushListenerService : NotificationListenerService() {

    companion object {
        private const val TAG = "PushListener"
        private const val PREFS = "pushlistener"
        private const val KEY_ENDPOINT = "endpoint"
        const val DEFAULT_ENDPOINT = "http://10.0.2.2:8787/ingest"
        private const val SPOOL = "spool.ndjson"
        private const val SPOOL_MAX_BYTES = 8L * 1024 * 1024
    }

    private val io = Executors.newSingleThreadExecutor()

    private fun endpoint(): String =
        getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .getString(KEY_ENDPOINT, DEFAULT_ENDPOINT) ?: DEFAULT_ENDPOINT

    override fun onListenerConnected() {
        super.onListenerConnected()
        Log.i(TAG, "listener connected; endpoint=${endpoint()}")
        // On (re)connect, forward anything already in the shade so a restart
        // does not lose alerts that arrived while we were unbound.
        try {
            activeNotifications?.forEach { handle(it, "active_on_connect") }
        } catch (e: Exception) {
            Log.w(TAG, "could not enumerate active notifications", e)
        }
        io.execute { flushSpool() }
    }

    override fun onNotificationPosted(sbn: StatusBarNotification) = handle(sbn, "posted")

    override fun onNotificationRemoved(sbn: StatusBarNotification) = handle(sbn, "removed")

    private fun handle(sbn: StatusBarNotification, event: String) {
        try {
            val json = toJson(sbn, event)
            io.execute { send(json) }
        } catch (e: Exception) {
            // A single malformed notification must never take the service down;
            // if it crashes, the OS unbinds us and capture stops silently.
            Log.e(TAG, "failed to handle notification from ${sbn.packageName}", e)
        }
    }

    private fun cs(v: CharSequence?): String? = v?.toString()

    private fun toJson(sbn: StatusBarNotification, event: String): String {
        val n: Notification = sbn.notification
        val x = n.extras

        val o = JSONObject()
        o.put("event", event)
        o.put("package", sbn.packageName)
        o.put("opPkg", sbn.opPkg)
        o.put("key", sbn.key)
        o.put("id", sbn.id)
        o.put("tag", sbn.tag ?: JSONObject.NULL)
        o.put("post_time_ms", sbn.postTime)
        o.put("received_at_ms", System.currentTimeMillis())
        o.put("user", sbn.user?.toString() ?: JSONObject.NULL)
        o.put("group_key", sbn.groupKey ?: JSONObject.NULL)
        o.put("is_clearable", sbn.isClearable)
        o.put("is_ongoing", sbn.isOngoing)
        o.put("channel_id", n.channelId ?: JSONObject.NULL)
        o.put("category", n.category ?: JSONObject.NULL)
        o.put("flags", n.flags)
        o.put("when_ms", n.`when`)

        // The fields that matter, straight from the extras bundle - no string
        // parsing, no redaction, no ambiguity about which build printed what.
        o.put("title", cs(x.getCharSequence(Notification.EXTRA_TITLE)) ?: JSONObject.NULL)
        o.put("text", cs(x.getCharSequence(Notification.EXTRA_TEXT)) ?: JSONObject.NULL)
        o.put("big_text", cs(x.getCharSequence(Notification.EXTRA_BIG_TEXT)) ?: JSONObject.NULL)
        o.put("sub_text", cs(x.getCharSequence(Notification.EXTRA_SUB_TEXT)) ?: JSONObject.NULL)
        o.put("summary_text", cs(x.getCharSequence(Notification.EXTRA_SUMMARY_TEXT)) ?: JSONObject.NULL)
        o.put("info_text", cs(x.getCharSequence(Notification.EXTRA_INFO_TEXT)) ?: JSONObject.NULL)
        o.put("template", x.getString(Notification.EXTRA_TEMPLATE) ?: JSONObject.NULL)

        // Ranking gives the importance the *system* assigned, which is not
        // always the channel's nominal importance.
        try {
            val rank = android.service.notification.NotificationListenerService.Ranking()
            if (currentRanking?.getRanking(sbn.key, rank) == true) {
                o.put("importance", rank.importance)
                o.put("matches_interruption_filter", rank.matchesInterruptionFilter())
            }
        } catch (_: Exception) { /* ranking is best-effort */ }

        return o.toString()
    }

    // ---------------------------------------------------------------------
    // Transport
    // ---------------------------------------------------------------------

    private fun send(line: String) {
        if (!post(line)) {
            spool(line)
        } else {
            flushSpool()
        }
    }

    private fun post(body: String): Boolean {
        var conn: HttpURLConnection? = null
        return try {
            conn = (URL(endpoint()).openConnection() as HttpURLConnection).apply {
                requestMethod = "POST"
                connectTimeout = 4000
                readTimeout = 4000
                doOutput = true
                setRequestProperty("Content-Type", "application/json; charset=utf-8")
            }
            BufferedWriter(OutputStreamWriter(conn.outputStream, Charsets.UTF_8)).use {
                it.write(body)
            }
            val code = conn.responseCode
            code in 200..299
        } catch (e: Exception) {
            Log.w(TAG, "post failed (${e.javaClass.simpleName}); spooling")
            false
        } finally {
            conn?.disconnect()
        }
    }

    private fun spoolFile() = File(filesDir, SPOOL)

    private fun spool(line: String) {
        try {
            val f = spoolFile()
            if (f.length() > SPOOL_MAX_BYTES) {
                Log.w(TAG, "spool full; dropping oldest half")
                val kept = f.readLines().let { it.subList(it.size / 2, it.size) }
                f.writeText(kept.joinToString("\n") + "\n")
            }
            f.appendText(line + "\n")
        } catch (e: Exception) {
            Log.e(TAG, "could not spool", e)
        }
    }

    private fun flushSpool() {
        val f = spoolFile()
        if (!f.exists() || f.length() == 0L) return
        try {
            val lines = f.readLines().filter { it.isNotBlank() }
            val remaining = mutableListOf<String>()
            var stop = false
            for (l in lines) {
                if (stop || !post(l)) {
                    stop = true          // host still down; keep the rest in order
                    remaining.add(l)
                }
            }
            if (remaining.isEmpty()) f.delete()
            else f.writeText(remaining.joinToString("\n") + "\n")
        } catch (e: Exception) {
            Log.e(TAG, "spool flush failed", e)
        }
    }
}
