package dev.phonenect

import android.util.Log

/**
 * С Android 10 фоновое приложение не может читать буфер, но при каждом копировании
 * ClipboardService пишет в журнал «Denying clipboard access to <наш пакет>». Эту строку
 * и ловим как сигнал «буфер изменился». Нужно разрешение READ_LOGS (выдаётся через ADB).
 */
class LogWatcher(private val packageName: String, private val onClipChanged: () -> Unit) : Thread("phonenect-logcat") {
    @Volatile
    private var process: Process? = null

    override fun run() {
        while (!isInterrupted) {
            val p = try {
                // -T 1: только новые строки; фильтр — лишь ClipboardService.
                Runtime.getRuntime().exec(arrayOf("logcat", "-T", "1", "-v", "brief", "ClipboardService:*", "*:S"))
            } catch (e: Exception) {
                Log.w("Phonenect", "logcat failed", e)
                return
            }
            process = p
            try {
                p.inputStream.bufferedReader().useLines { lines ->
                    for (line in lines) {
                        if (line.contains(packageName) && line.contains("clipboard access", ignoreCase = true)) {
                            onClipChanged()
                        }
                    }
                }
            } catch (_: Exception) {
            } finally {
                p.destroy()
            }
            try {
                sleep(1000)
            } catch (_: InterruptedException) {
                return
            }
        }
    }

    /** Чтение журнала блокирует поток — прерываем, убивая сам процесс logcat. */
    fun shutdown() {
        interrupt()
        process?.destroy()
    }
}
