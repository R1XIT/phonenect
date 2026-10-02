package dev.phonenect

import android.content.ContentProvider
import android.content.ContentValues
import android.content.Context
import android.database.Cursor
import android.database.MatrixCursor
import android.net.Uri
import android.os.Environment
import android.os.ParcelFileDescriptor
import android.provider.OpenableColumns
import android.webkit.MimeTypeMap
import java.io.File

/** Отдаёт картинки, пришедшие с ПК, приложениям, которые вставляют их из буфера, и скачанные файлы на Android 8–9. */
class ImageProvider : ContentProvider() {
    companion object {
        fun dir(context: Context) = File(context.cacheDir, "clips").apply { mkdirs() }

        fun uriFor(context: Context, file: File): Uri =
            Uri.parse("content://${context.packageName}.images/${file.name}")

        /** Скачанный с ПК файл на Android 8–9 (там он лежит в папке приложения). Имя кодируем: в нём бывают пробелы и «#». */
        fun uriForDownload(context: Context, file: File): Uri =
            Uri.Builder().scheme("content").authority("${context.packageName}.images")
                .appendPath("downloads").appendPath(file.name).build()

        fun downloads(context: Context) = File(context.getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS), "Phonenect")
    }

    override fun onCreate() = true

    private fun file(uri: Uri): File {
        val name = uri.lastPathSegment ?: throw IllegalArgumentException(uri.toString())
        val dir = if (uri.pathSegments.firstOrNull() == "downloads") downloads(context!!) else dir(context!!)
        val f = File(dir, name)
        require(f.canonicalPath.startsWith(dir.canonicalPath + File.separator)) { "bad path" }
        return f
    }

    override fun getType(uri: Uri) =
        MimeTypeMap.getSingleton().getMimeTypeFromExtension(file(uri).extension.lowercase()) ?: "application/octet-stream"

    override fun openFile(uri: Uri, mode: String): ParcelFileDescriptor =
        ParcelFileDescriptor.open(file(uri), ParcelFileDescriptor.MODE_READ_ONLY)

    override fun query(uri: Uri, projection: Array<out String>?, s: String?, a: Array<out String>?, o: String?): Cursor {
        val f = file(uri)
        return MatrixCursor(arrayOf(OpenableColumns.DISPLAY_NAME, OpenableColumns.SIZE)).apply {
            addRow(arrayOf<Any>(f.name, f.length()))
        }
    }

    override fun insert(uri: Uri, values: ContentValues?): Uri? = null
    override fun delete(uri: Uri, s: String?, a: Array<out String>?) = 0
    override fun update(uri: Uri, v: ContentValues?, s: String?, a: Array<out String>?) = 0
}
