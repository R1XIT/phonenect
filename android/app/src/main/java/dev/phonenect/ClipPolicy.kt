package dev.phonenect

import android.os.PersistableBundle

/** Что из буфера Android нельзя отправлять на ПК. */
object ClipPolicy {
    // Значение ClipDescription.EXTRA_IS_SENSITIVE (API 33); строкой, потому что minSdk ниже.
    private const val EXTRA_IS_SENSITIVE = "android.content.extra.IS_SENSITIVE"

    /** Менеджеры паролей (Android 13+) помечают скопированное секретным. */
    fun isSensitive(extras: PersistableBundle?): Boolean =
        extras?.getBoolean(EXTRA_IS_SENSITIVE, false) == true
}
