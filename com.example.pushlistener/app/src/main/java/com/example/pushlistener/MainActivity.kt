package com.example.pushlistener

import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.provider.Settings
import android.widget.Button
import android.widget.EditText
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity

/**
 * One screen: shows whether notification access is granted, opens the settings
 * page where the user grants it, and lets the collector endpoint be changed.
 *
 * Notification access cannot be granted programmatically. The button below
 * only *opens* the settings screen; a human still has to flip the toggle.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var status: TextView
    private lateinit var endpoint: EditText

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        status = findViewById(R.id.status)
        endpoint = findViewById(R.id.endpoint)

        val prefs = getSharedPreferences("pushlistener", Context.MODE_PRIVATE)
        endpoint.setText(
            prefs.getString("endpoint", PushListenerService.DEFAULT_ENDPOINT)
        )

        findViewById<Button>(R.id.grant).setOnClickListener {
            startActivity(Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS))
        }
        findViewById<Button>(R.id.save).setOnClickListener {
            prefs.edit().putString("endpoint", endpoint.text.toString().trim()).apply()
            status.text = "Endpoint saved. Toggle notification access off and on to reconnect."
        }
    }

    override fun onResume() {
        super.onResume()
        status.text = if (isEnabled()) {
            "Notification access GRANTED. Capturing."
        } else {
            "Notification access NOT granted.\nTap the button below, then enable " +
                "\"Push Observatory Listener\"."
        }
    }

    private fun isEnabled(): Boolean {
        val flat = Settings.Secure.getString(
            contentResolver, "enabled_notification_listeners"
        ) ?: return false
        return flat.split(":").any { it.contains(packageName) }
    }
}
