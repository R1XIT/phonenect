package dev.phonenect

import java.net.URI
import java.net.URLDecoder
import java.security.MessageDigest

/** Компьютер с Phonenect. id — как config.pc_id на ПК: по нему ПК находится в сети после смены IP. */
data class Pc(val id: String, val name: String, val url: String, val token: String, val fp: String, val ca: String = "")

/** Подключён до шифрования (или CA ещё не получен): служба к такому ПК не подключается, экран просит переподключить. */
val Pc.needsRepair: Boolean get() = fp.isEmpty() || ca.isEmpty() || !url.startsWith("https://")

/** Список ПК, с которыми связан телефон; буфер общий с одним, активным. Без Android — чтобы проверять тестами. */
data class PcList(val items: List<Pc> = emptyList(), val activeId: String? = null) {
    val active: Pc? get() = items.firstOrNull { it.id == activeId }

    /** Новый ПК становится активным; уже известный (тот же токен) обновляет адрес и имя. */
    fun add(pc: Pc): PcList {
        val known = items.firstOrNull { it.id == pc.id }
            ?: return replaceRotated(pc) ?: PcList(items + pc, pc.id)
        // В адресе API имени нет — вместо него хост; известное имя не затираем.
        val name = if (pc.name == hostOf(pc.url)) known.name else pc.name
        // Уже полученный CA сохраняем, если отпечаток тот же; иначе берём CA нового.
        val ca = if (pc.ca.isEmpty() && pc.fp == known.fp) known.ca else pc.ca
        return PcList(items.map { if (it.id == pc.id) pc.copy(name = name, ca = ca) else it }, pc.id)
    }

    /**
     * ПК сменил токен («Отключить все устройства»), а CA остался: тот же отпечаток при другом id — тот же ПК.
     * Старая запись заменяется на месте, а не остаётся рядом отключённой.
     */
    private fun replaceRotated(pc: Pc): PcList? {
        if (pc.fp.isEmpty()) return null
        val old = items.firstOrNull { it.fp == pc.fp } ?: return null
        val name = if (pc.name == hostOf(pc.url)) old.name else pc.name
        val ca = if (pc.ca.isEmpty()) old.ca else pc.ca
        return PcList(items.map { if (it.id == old.id) pc.copy(name = name, ca = ca) else it }, pc.id)
    }

    fun activate(id: String) = if (items.any { it.id == id }) copy(activeId = id) else this

    fun remove(id: String): PcList {
        val rest = items.filter { it.id != id }
        return PcList(rest, if (activeId == id) rest.firstOrNull()?.id else activeId)
    }

    fun moved(id: String, host: String, port: Int) = update(id) { it.copy(url = "https://$host:$port") }

    fun renamed(id: String, name: String) = update(id) { it.copy(name = name) }

    private fun update(id: String, change: (Pc) -> Pc) = copy(items = items.map { if (it.id == id) change(it) else it })

    companion object {
        fun idFor(token: String): String =
            MessageDigest.getInstance("SHA-256").digest(token.toByteArray()).joinToString("") { "%02x".format(it) }.take(12)

        /**
         * phonenect://pair?url=https://host:port&t=TOKEN&fp=…&name=… (кнопка в веб-клиенте)
         * или адрес API со страницы: https://host:port/api/clip?t=TOKEN&fp=….
         */
        fun parseLink(link: String): Pc? {
            val uri = try {
                URI(link.trim())
            } catch (e: Exception) {
                return null
            }
            val query = (uri.rawQuery ?: "").split("&").mapNotNull {
                val (k, v) = it.split("=", limit = 2).takeIf { p -> p.size == 2 } ?: return@mapNotNull null
                k to URLDecoder.decode(v, "UTF-8")
            }.toMap()
            val token = query["t"]?.takeIf { it.isNotEmpty() } ?: return null
            val fp = query["fp"]?.lowercase()?.takeIf { FP.matches(it) } ?: return null
            val base = when (uri.scheme) {
                "phonenect" -> query["url"]?.takeIf { it.startsWith("https://") }
                "https" -> if (uri.rawAuthority != null) "https://${uri.rawAuthority}" else null
                else -> null
            }?.trimEnd('/') ?: return null
            return Pc(idFor(token), query["name"]?.takeIf { it.isNotBlank() } ?: hostOf(base), base, token, fp)
        }

        private val FP = Regex("[0-9a-f]{64}")

        /** Ссылка Phonenect, но без шифрования (или без отпечатка): из старой версии. */
        fun isOldLink(link: String): Boolean {
            val text = link.trim()
            return parseLink(text) == null && Regex("[?&]t=").containsMatchIn(text) &&
                listOf("http://", "https://", "phonenect://").any { text.startsWith(it) }
        }

        fun hostOf(url: String): String = try {
            URI(url).host
        } catch (e: Exception) {
            null
        } ?: url
    }
}
