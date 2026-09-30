package dev.phonenect

import android.content.ContentProvider
import android.content.ContentValues
import android.content.Context
import android.database.Cursor
import android.database.MatrixCursor
import android.net.Uri
import android.os.ParcelFileDescriptor
import android.provider.OpenableColumns
import java.io.File

/** Отдаёт картинки, пришедшие с ПК, приложениям, которые вставляют их из буфера. */
class ImageProvider : ContentProvider() {
    companion object {
        fun dir(context: Context) = File(context.cacheDir, "clips").apply { mkdirs() }

        fun uriFor(context: Context, file: File): Uri =
            Uri.parse("content://${context.packageName}.images/${file.name}")

        private val MIME = mapOf("png" to "image/png", "jpg" to "image/jpeg", "gif" to "image/gif", "webp" to "image/webp")
    }

    override fun onCreate() = true

    private fun file(uri: Uri): File {
        val name = uri.lastPathSegment ?: throw IllegalArgumentException(uri.toString())
        val f = File(dir(context!!), name)
        require(f.canonicalPath.startsWith(dir(context!!).canonicalPath)) { "bad path" }
        return f
    }

    override fun getType(uri: Uri) = MIME[file(uri).extension] ?: "image/png"

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
