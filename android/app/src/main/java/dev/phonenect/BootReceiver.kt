package dev.phonenect

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/** После перезагрузки и обновления приложения синхронизация включается сама. */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (Prefs(context).configured) SyncService.start(context)
    }
}
