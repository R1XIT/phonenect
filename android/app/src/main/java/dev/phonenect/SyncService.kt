package dev.phonenect

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.SystemClock
import android.provider.Settings
import android.util.Log
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONObject
import java.io.File
import java.security.MessageDigest
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

/**
 * Держит связь с агентом на ПК.
 * ПК → телефон: новый клип по WebSocket сразу кладётся в буфер (запись в фоне Android разрешает).
 * Телефон → ПК: копирование в фоне ловит [LogWatcher], буфер читает [ClipReaderActivity].
 */
class SyncService : Service() {
    companion object {
        private const val TAG = "Phonenect"
        private const val CHANNEL = "sync"
        private const val NOTIFICATION_ID = 1

        @Volatile
        var instance: SyncService? = null
            private set

        fun start(context: Context) {
            context.startForegroundService(Intent(context, SyncService::class.java))
        }

        fun stop(context: Context) {
            context.stopService(Intent(context, SyncService::class.java))
        }

        val deviceName: String = Build.MODEL.take(40)
    }

    private val http = OkHttpClient.Builder()
        .pingInterval(20, TimeUnit.SECONDS)
        .connectTimeout(5, TimeUnit.SECONDS)
        .build()
    private val io = Executors.newSingleThreadExecutor()
    private val main = Handler(Looper.getMainLooper())
    private lateinit var prefs: Prefs
    private lateinit var clipboard: ClipboardManager
    private var socket: WebSocket? = null
    private var logWatcher: LogWatcher? = null
    private var failures = 0
    private var stopped = false

    @Volatile
    var connected = false
        private set

    // Защита от эха: то, что мы сами положили в буфер, обратно не отправляем.
    @Volatile
    private var ownDigest: String? = null
    @Volatile
    private var ownSetAt = 0L

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        instance = this
        prefs = Prefs(this)
        clipboard = getSystemService(ClipboardManager::class.java)
        getSystemService(NotificationManager::class.java).createNotificationChannel(
            NotificationChannel(CHANNEL, getString(R.string.channel_name), NotificationManager.IMPORTANCE_MIN)
        )
        val notification = notification(getString(R.string.status_connecting))
        if (Build.VERSION.SDK_INT >= 34) {
            startForeground(NOTIFICATION_ID, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
        } else {
            startForeground(NOTIFICATION_ID, notification)
        }
        // Пока приложение на экране, изменения буфера приходят и сюда.
        clipboard.addPrimaryClipChangedListener(clipListener)
        startLogWatcher()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (!prefs.configured) {
            stopSelf()
            return START_NOT_STICKY
        }
        if (socket == null) connect()
        return START_STICKY
    }

    override fun onDestroy() {
        stopped = true
        instance = null
        logWatcher?.shutdown()
        socket?.close(1000, null)
        clipboard.removePrimaryClipChangedListener(clipListener)
        super.onDestroy()
    }

    // ---------- автоотправка из фона ----------

    val canReadLogs: Boolean
        get() = checkSelfPermission(android.Manifest.permission.READ_LOGS) == PackageManager.PERMISSION_GRANTED

    val canOpenFromBackground: Boolean get() = Settings.canDrawOverlays(this)

    private fun startLogWatcher() {
        if (!canReadLogs || logWatcher != null) return
        logWatcher = LogWatcher(packageName) {
            // Своё только что положенное не читаем — иначе мигали бы окном при каждом клипе с ПК.
            if (SystemClock.elapsedRealtime() - ownSetAt < 1500) return@LogWatcher
            main.post { ClipReaderActivity.launch(this) }
        }.also { it.start() }
    }

    private val clipListener = ClipboardManager.OnPrimaryClipChangedListener {
        clipboard.primaryClip?.let { sendClip(it) }
    }

    // ---------- связь с ПК ----------

    private fun connect() {
        val base = prefs.baseUrl ?: return
        val request = Request.Builder()
            .url(base.replaceFirst("http", "ws") + "/ws")
            .header("Authorization", "Bearer ${prefs.token}")
            .build()
        socket = http.newWebSocket(request, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                failures = 0
                connected = true
                updateStatus(getString(R.string.status_connected, base.removePrefix("http://")))
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                val msg = JSONObject(text)
                if (msg.optString("event") != "clip") return
                val clip = msg.getJSONObject("clip")
                if (clip.optString("source") == deviceName) return
                receive(clip)
            }

            override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                webSocket.close(1000, null)
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) = reconnect()

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                Log.w(TAG, "ws failure: ${t.message}")
                reconnect()
            }
        })
    }

    private fun reconnect() {
        connected = false
        socket = null
        if (stopped) return
        failures++
        updateStatus(getString(R.string.status_offline))
        // После нескольких неудач ищем ПК в сети заново: он мог получить другой IP.
        if (failures % 3 == 0) Discovery.find(this) { host, port -> prefs.replaceHost(host, port) }
        val delay = minOf(30_000L, 2_000L * failures)
        main.postDelayed({ if (!stopped && socket == null) connect() }, delay)
    }

    private fun receive(meta: JSONObject) {
        when (meta.optString("kind")) {
            "text" -> setClip(ClipData.newPlainText("Phonenect", meta.optString("text")), digest(meta.optString("text").toByteArray()))
            "image" -> io.execute {
                try {
                    val bytes = get("/api/clip/${meta.getInt("id")}") ?: return@execute
                    val ext = when (meta.optString("mime")) {
                        "image/jpeg" -> "jpg"; "image/gif" -> "gif"; "image/webp" -> "webp"; else -> "png"
                    }
                    ImageProvider.dir(this).listFiles()?.forEach { it.delete() }
                    val file = File(ImageProvider.dir(this), "clip-${meta.getInt("id")}.$ext").apply { writeBytes(bytes) }
                    val uri = ImageProvider.uriFor(this, file)
                    main.post { setClip(ClipData.newUri(contentResolver, "Phonenect", uri), digest(bytes)) }
                } catch (e: Exception) {
                    Log.w(TAG, "image fetch failed", e)
                }
            }
        }
    }

    private fun setClip(clip: ClipData, digest: String) {
        ownDigest = digest
        ownSetAt = SystemClock.elapsedRealtime()
        clipboard.setPrimaryClip(clip)
    }

    /** Отправить содержимое буфера на ПК. Повторы сервер отбрасывает сам (X-Auto). */
    fun sendClip(clip: ClipData) {
        if (clip.itemCount == 0) return
        val item = clip.getItemAt(0)
        io.execute {
            try {
                val uri = item.uri
                val mime = uri?.let { contentResolver.getType(it) }
                val (body, type) = if (uri != null && mime != null && mime.startsWith("image/")) {
                    val bytes = contentResolver.openInputStream(uri)?.use { it.readBytes() } ?: return@execute
                    bytes to mime
                } else {
                    val text = item.coerceToText(this)?.toString().orEmpty()
                    if (text.isEmpty()) return@execute
                    text.toByteArray() to "text/plain; charset=utf-8"
                }
                if (digest(body) == ownDigest) return@execute
                post(body, type)
            } catch (e: Exception) {
                Log.w(TAG, "send failed", e)
            }
        }
    }

    private fun get(path: String): ByteArray? {
        val request = Request.Builder().url(prefs.baseUrl + path)
            .header("Authorization", "Bearer ${prefs.token}").build()
        http.newCall(request).execute().use { r -> return if (r.isSuccessful) r.body.bytes() else null }
    }

    private fun post(body: ByteArray, type: String) {
        val request = Request.Builder().url(prefs.baseUrl + "/api/clip")
            .header("Authorization", "Bearer ${prefs.token}")
            .header("X-Device", deviceName)
            .header("X-Auto", "1")
            .post(body.toRequestBody(type.toMediaType()))
            .build()
        http.newCall(request).execute().close()
    }

    // ---------- уведомление ----------

    private fun notification(text: String): Notification {
        val open = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE
        )
        return Notification.Builder(this, CHANNEL)
            .setSmallIcon(R.drawable.ic_notification)
            .setContentTitle(getString(R.string.app_name))
            .setContentText(text)
            .setContentIntent(open)
            .setOngoing(true)
            .build()
    }

    private fun updateStatus(text: String) {
        getSystemService(NotificationManager::class.java).notify(NOTIFICATION_ID, notification(text))
        MainActivity.refresh()
    }

    private fun digest(bytes: ByteArray): String =
        MessageDigest.getInstance("SHA-1").digest(bytes).joinToString("") { "%02x".format(it) }
}
