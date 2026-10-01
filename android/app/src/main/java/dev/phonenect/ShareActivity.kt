package dev.phonenect

import android.app.Activity
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.widget.Toast

/** «Поделиться» → Phonenect: файлы уходят в папку на ПК, текст — в буфер ПК. */
class ShareActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        if (!Prefs(this).configured) {
            Toast.makeText(this, "Сначала подключите Phonenect к ПК", Toast.LENGTH_LONG).show()
            startActivity(Intent(this, MainActivity::class.java))
        } else {
            val uris = streams(intent)
            val text = intent.getStringExtra(Intent.EXTRA_TEXT)
            when {
                uris.isNotEmpty() -> {
                    SyncService.sendFiles(this, uris)
                    Toast.makeText(this, "Отправляю на ПК…", Toast.LENGTH_SHORT).show()
                }
                !text.isNullOrEmpty() -> {
                    SyncService.sendText(this, text)
                    Toast.makeText(this, "Отправлено на ПК", Toast.LENGTH_SHORT).show()
                }
            }
        }
        finish()
    }

    @Suppress("DEPRECATION")
    private fun streams(intent: Intent): List<Uri> = when (intent.action) {
        Intent.ACTION_SEND -> listOfNotNull(
            if (Build.VERSION.SDK_INT >= 33) intent.getParcelableExtra(Intent.EXTRA_STREAM, Uri::class.java)
            else intent.getParcelableExtra(Intent.EXTRA_STREAM)
        )
        Intent.ACTION_SEND_MULTIPLE ->
            (if (Build.VERSION.SDK_INT >= 33) intent.getParcelableArrayListExtra(Intent.EXTRA_STREAM, Uri::class.java)
            else intent.getParcelableArrayListExtra(Intent.EXTRA_STREAM)).orEmpty()
        else -> emptyList()
    }
}
