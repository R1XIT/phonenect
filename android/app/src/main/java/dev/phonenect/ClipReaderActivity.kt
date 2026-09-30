package dev.phonenect

import android.app.Activity
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent

/**
 * Невидимое окно: Android даёт читать буфер только приложению в фокусе.
 * Открываемся на мгновение, читаем буфер, отдаём службе и сразу закрываемся.
 */
class ClipReaderActivity : Activity() {
    companion object {
        fun launch(context: Context) {
            context.startActivity(
                Intent(context, ClipReaderActivity::class.java).addFlags(
                    Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_NO_ANIMATION or
                        Intent.FLAG_ACTIVITY_EXCLUDE_FROM_RECENTS or Intent.FLAG_ACTIVITY_MULTIPLE_TASK
                )
            )
        }
    }

    override fun onWindowFocusChanged(hasFocus: Boolean) {
        super.onWindowFocusChanged(hasFocus)
        if (!hasFocus) return
        getSystemService(ClipboardManager::class.java).primaryClip?.let { SyncService.instance?.sendClip(it) }
        finish()
    }

    override fun finish() {
        super.finish()
        @Suppress("DEPRECATION")
        overridePendingTransition(0, 0)
    }
}
