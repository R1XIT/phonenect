package dev.phonenect

import android.content.Context
import android.net.Uri

/** Адрес агента на ПК и токен. Задаются по ссылке phonenect://pair или вставкой адреса API. */
class Prefs(context: Context) {
    private val sp = context.getSharedPreferences("phonenect", Context.MODE_PRIVATE)

    var baseUrl: String?
        get() = sp.getString("base_url", null)
        set(value) = sp.edit().putString("base_url", value).apply()

    var token: String?
        get() = sp.getString("token", null)
        set(value) = sp.edit().putString("token", value).apply()

    val configured: Boolean get() = !baseUrl.isNullOrEmpty() && !token.isNullOrEmpty()

    /**
     * Принимает phonenect://pair?url=http://host:port&t=TOKEN
     * или адрес API из веб-клиента: http://host:port/api/clip?t=TOKEN.
     */
    fun applyLink(link: String): Boolean {
        val uri = Uri.parse(link.trim())
        val token = uri.getQueryParameter("t") ?: return false
        val base = when (uri.scheme) {
            "phonenect" -> uri.getQueryParameter("url")
            "http", "https" -> "${uri.scheme}://${uri.authority}"
            else -> null
        } ?: return false
        baseUrl = base.trimEnd('/')
        this.token = token
        return true
    }

    /** ПК сменил IP — меняем только хост, порт и токен прежние. */
    fun replaceHost(host: String, port: Int) {
        baseUrl = "http://$host:$port"
    }
}
