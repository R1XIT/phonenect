package dev.phonenect

import android.app.DownloadManager
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
import android.net.Uri
import android.os.Build
import android.os.Environment
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.SystemClock
import android.provider.OpenableColumns
import android.provider.Settings
import android.text.format.Formatter
import android.util.Log
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MediaType.Companion.toMediaTypeOrNull
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.BufferedSink
import okio.source
import org.json.JSONObject
import java.io.File
import java.io.IOException
import java.net.URLEncoder
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
        private const val FILES_CHANNEL = "files"
        private const val NOTIFICATION_ID = 1
        private const val UPLOAD_NOTIFICATION_ID = 2
        private const val ACTION_SEND_FILES = "dev.phonenect.SEND_FILES"
        private const val ACTION_SEND_TEXT = "dev.phonenect.SEND_TEXT"
        private const val ACTION_DOWNLOAD = "dev.phonenect.DOWNLOAD"
        // Файлы с ПК до этого размера скачиваются сами, большие — по нажатию на уведомление.
        private const val AUTO_DOWNLOAD_LIMIT = 200L * 1024 * 1024

        @Volatile
        var instance: SyncService? = null
            private set

        fun start(context: Context) {
            context.startForegroundService(Intent(context, SyncService::class.java))
        }

        /** Отправить файлы на ПК. Доступ к ним передаём службе вместе с намерением. */
        fun sendFiles(context: Context, uris: List<Uri>) {
            val clip = ClipData.newRawUri("Phonenect", uris[0]).apply { uris.drop(1).forEach { addItem(ClipData.Item(it)) } }
            context.startForegroundService(
                Intent(context, SyncService::class.java).setAction(ACTION_SEND_FILES).apply {
                    clipData = clip
                    addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
                }
            )
        }

        fun sendText(context: Context, text: String) {
            context.startForegroundService(
                Intent(context, SyncService::class.java).setAction(ACTION_SEND_TEXT).putExtra(Intent.EXTRA_TEXT, text)
            )
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
    private val uploads = Executors.newSingleThreadExecutor() // большие файлы не задерживают буфер
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
        getSystemService(NotificationManager::class.java).createNotificationChannels(
            listOf(
                NotificationChannel(CHANNEL, getString(R.string.channel_name), NotificationManager.IMPORTANCE_MIN),
                NotificationChannel(FILES_CHANNEL, getString(R.string.files_channel_name), NotificationManager.IMPORTANCE_DEFAULT),
            )
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
        if (intent == null) return START_STICKY
        when (intent.action) {
            ACTION_SEND_FILES -> intent.clipData?.let { clip ->
                val uris = (0 until clip.itemCount).mapNotNull { clip.getItemAt(it).uri }
                uploads.execute { uploadAll(uris) }
            }
            ACTION_SEND_TEXT -> intent.getStringExtra(Intent.EXTRA_TEXT)?.let { text ->
                io.execute {
                    try {
                        post(text.toByteArray(), "text/plain; charset=utf-8", auto = false)
                    } catch (e: Exception) {
                        Log.w(TAG, "send text failed", e)
                    }
                }
            }
            ACTION_DOWNLOAD -> {
                val id = intent.getIntExtra("id", 0)
                getSystemService(NotificationManager::class.java).cancel(1000 + id)
                download(id, intent.getStringExtra("name").orEmpty(), intent.getStringExtra("mime"))
            }
        }
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
            "file" -> {
                val id = meta.getInt("id")
                val name = meta.optString("name", "Файл")
                val mime = meta.optString("mime", "application/octet-stream")
                val size = meta.optLong("size")
                if (size <= AUTO_DOWNLOAD_LIMIT) download(id, name, mime) else offerDownload(id, name, mime, size)
            }
        }
    }

    // ---------- файлы ----------

    /** Качает файл с ПК в «Загрузки/Phonenect»; прогресс и «Открыть» показывает сам Android. */
    private fun download(id: Int, name: String, mime: String?) {
        val base = prefs.baseUrl ?: return
        val safe = name.replace(Regex("[\\\\/:*?\"<>|]"), "_").ifBlank { "Файл" }
        val request = DownloadManager.Request(Uri.parse("$base/api/clip/$id"))
            .addRequestHeader("Authorization", "Bearer ${prefs.token}")
            .setTitle(safe)
            .setDescription(getString(R.string.download_description))
            .setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED)
        if (mime != null) request.setMimeType(mime)
        try {
            request.setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, "Phonenect/$safe")
        } catch (e: Exception) {
            // До Android 10 общая папка требует разрешения на память — тогда кладём в папку приложения.
            request.setDestinationInExternalFilesDir(this, Environment.DIRECTORY_DOWNLOADS, safe)
        }
        try {
            getSystemService(DownloadManager::class.java).enqueue(request)
        } catch (e: Exception) {
            Log.w(TAG, "download failed", e)
        }
    }

    private fun offerDownload(id: Int, name: String, mime: String, size: Long) {
        val intent = Intent(this, SyncService::class.java).setAction(ACTION_DOWNLOAD)
            .putExtra("id", id).putExtra("name", name).putExtra("mime", mime)
        val tap = PendingIntent.getForegroundService(
            this, id, intent, PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )
        val notification = Notification.Builder(this, FILES_CHANNEL)
            .setSmallIcon(R.drawable.ic_notification)
            .setContentTitle(getString(R.string.file_offer_title, name))
            .setContentText(getString(R.string.file_offer_text, Formatter.formatShortFileSize(this, size)))
            .setContentIntent(tap)
            .setAutoCancel(true)
            .build()
        getSystemService(NotificationManager::class.java).notify(1000 + id, notification)
    }

    private fun uploadAll(uris: List<Uri>) {
        var sent = 0
        var error: String? = null
        for ((i, uri) in uris.withIndex()) {
            val name = displayName(uri)
            val counter = if (uris.size > 1) " (${i + 1} из ${uris.size})" else ""
            uploadStatus(getString(R.string.upload_progress) + counter, name, ongoing = true)
            try {
                upload(uri, name)
                sent++
            } catch (e: Exception) {
                Log.w(TAG, "upload failed", e)
                error = "$name: ${e.message}"
            }
        }
        when {
            error != null -> uploadStatus(getString(R.string.upload_failed), error, ongoing = false)
            sent == 1 -> uploadStatus(getString(R.string.upload_done), displayName(uris[0]), ongoing = false)
            else -> uploadStatus(getString(R.string.upload_done), getString(R.string.upload_count, sent), ongoing = false)
        }
    }

    private fun upload(uri: Uri, name: String) {
        val mime = contentResolver.getType(uri) ?: "application/octet-stream"
        // Читаем файл прямо в сеть, не загружая в память целиком.
        val body = object : RequestBody() {
            override fun contentType() = mime.toMediaTypeOrNull()
            override fun writeTo(sink: BufferedSink) {
                val input = contentResolver.openInputStream(uri) ?: throw IOException("нет доступа к файлу")
                input.source().use { sink.writeAll(it) }
            }
        }
        val request = Request.Builder().url(prefs.baseUrl + "/api/clip")
            .header("Authorization", "Bearer ${prefs.token}")
            .header("X-Device", deviceName)
            .header("X-Filename", URLEncoder.encode(name, "UTF-8").replace("+", "%20"))
            .post(body)
            .build()
        http.newCall(request).execute().use { r ->
            if (!r.isSuccessful) throw IOException("ПК ответил ${r.code}")
        }
    }

    private fun displayName(uri: Uri): String =
        try {
            contentResolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME), null, null, null)?.use { c ->
                if (c.moveToFirst()) c.getString(0) else null
            }
        } catch (e: Exception) {
            null
        } ?: uri.lastPathSegment ?: "Файл"

    private fun uploadStatus(title: String, text: String, ongoing: Boolean) {
        val notification = Notification.Builder(this, FILES_CHANNEL)
            .setSmallIcon(R.drawable.ic_notification)
            .setContentTitle(title)
            .setContentText(text)
            .setOngoing(ongoing)
            .setOnlyAlertOnce(true)
            .apply { if (ongoing) setProgress(0, 0, true) }
            .build()
        getSystemService(NotificationManager::class.java).notify(UPLOAD_NOTIFICATION_ID, notification)
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

    private fun post(body: ByteArray, type: String, auto: Boolean = true) {
        val request = Request.Builder().url(prefs.baseUrl + "/api/clip")
            .header("Authorization", "Bearer ${prefs.token}")
            .header("X-Device", deviceName)
            .apply { if (auto) header("X-Auto", "1") }
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
