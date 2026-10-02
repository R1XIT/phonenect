package dev.phonenect

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject

/** Компьютеры, к которым подключён телефон, и какой из них активный. Задаются по ссылке phonenect://pair или вставкой адреса API. */
class Prefs(context: Context) {
    private companion object {
        // Prefs создают и экран, и служба (её потоки тоже) — общий замок на «прочитать-изменить-записать».
        val LOCK = Any()
    }

    private val sp = context.getSharedPreferences("phonenect", Context.MODE_PRIVATE)

    var pcs: PcList
        get() = synchronized(LOCK) {
            migrate()
            val array = JSONArray(sp.getString("pcs", "[]"))
            val items = (0 until array.length()).map { i ->
                array.getJSONObject(i).run { Pc(getString("id"), getString("name"), getString("url"), getString("token"), optString("fp"), optString("ca")) }
            }
            PcList(items, sp.getString("active", null))
        }
        set(value) = synchronized(LOCK) {
            val array = JSONArray(value.items.map { pc ->
                JSONObject().put("id", pc.id).put("name", pc.name).put("url", pc.url).put("token", pc.token)
                    .put("fp", pc.fp).put("ca", pc.ca)
            })
            sp.edit().putString("pcs", array.toString()).putString("active", value.activeId).apply()
        }

    private fun update(change: (PcList) -> PcList) = synchronized(LOCK) { pcs = change(pcs) }

    val active: Pc? get() = pcs.active
    val baseUrl: String? get() = active?.url
    val token: String? get() = active?.token
    val configured: Boolean get() = active != null

    /** Добавляет ПК по ссылке и делает его активным. null — ссылка не от Phonenect. */
    fun applyLink(link: String): Pc? {
        val pc = PcList.parseLink(link) ?: return null
        update { it.add(pc) }
        return pc
    }

    /** ПК сменил IP — меняем только хост, порт и токен прежние. */
    fun replaceHost(id: String, host: String, port: Int) {
        update { it.moved(id, host, port) }
    }

    fun rename(id: String, name: String) {
        update { it.renamed(id, name) }
    }

    fun activate(id: String) {
        update { it.activate(id) }
    }

    fun remove(id: String) {
        update { it.remove(id) }
    }

    /** До списка ПК хранились один адрес и токен. */
    private fun migrate() {
        val url = sp.getString("base_url", null) ?: return
        val token = sp.getString("token", null)
        sp.edit().remove("base_url").remove("token").apply()
        if (token.isNullOrEmpty()) return
        val host = url.substringAfter("://").substringBefore(":")
        pcs = PcList().add(Pc(PcList.idFor(token), host, url, token, fp = ""))
    }
}
