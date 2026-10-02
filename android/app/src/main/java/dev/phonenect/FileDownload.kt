package dev.phonenect

import android.content.ContentValues
import android.content.Context
import android.net.Uri
import android.os.Build
import android.os.Environment
import android.provider.MediaStore
import okhttp3.OkHttpClient
import okhttp3.Request
import java.io.File
import java.io.IOException

/** Файл с ПК — в «Загрузки/Phonenect». Качаем сами: системный загрузчик не знает сертификата ПК. */
object FileDownload {
    fun save(context: Context, client: OkHttpClient, request: Request, name: String, mime: String?): Uri {
        client.newCall(request).execute().use { response ->
            if (!response.isSuccessful) throw IOException("ПК ответил ${response.code}")
            val body = response.body
            return if (Build.VERSION.SDK_INT >= 29) {
                val values = ContentValues().apply {
                    put(MediaStore.Downloads.DISPLAY_NAME, name)
                    put(MediaStore.Downloads.MIME_TYPE, mime ?: "application/octet-stream")
                    put(MediaStore.Downloads.RELATIVE_PATH, Environment.DIRECTORY_DOWNLOADS + "/Phonenect")
                    put(MediaStore.Downloads.IS_PENDING, 1)
                }
                val resolver = context.contentResolver
                val uri = resolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values)
                    ?: throw IOException("Не удалось создать файл в «Загрузках»")
                try {
                    resolver.openOutputStream(uri)!!.use { out -> body.byteStream().copyTo(out) }
                    resolver.update(uri, ContentValues().apply { put(MediaStore.Downloads.IS_PENDING, 0) }, null, null)
                    uri
                } catch (e: Exception) {
                    resolver.delete(uri, null, null)
                    throw e
                }
            } else {
                // До Android 10 общая папка требует разрешения на память — кладём в папку приложения.
                val dir = ImageProvider.downloads(context).apply { mkdirs() }
                val file = File(dir, name)
                try {
                    file.outputStream().use { out -> body.byteStream().copyTo(out) }
                } catch (e: Exception) {
                    file.delete() // недокачанный файл не оставляем
                    throw e
                }
                ImageProvider.uriForDownload(context, file)
            }
        }
    }
}
