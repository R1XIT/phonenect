package dev.phonenect

import okhttp3.OkHttpClient
import okhttp3.Request
import java.io.IOException
import java.security.GeneralSecurityException
import java.security.KeyStore
import java.security.MessageDigest
import java.security.cert.CertificateFactory
import java.security.cert.X509Certificate
import javax.net.ssl.SSLContext
import javax.net.ssl.TrustManagerFactory
import javax.net.ssl.X509TrustManager

/** Доверие только CA своего ПК: его отпечаток приходит в ссылке подключения (QR или USB). */
object Tls {
    class Mismatch : Exception("Сертификат не совпадает — подключайтесь из дома и проверьте отпечаток на странице ПК")

    /** ПК не ответил или ответил не сертификатом. */
    class Unreachable(cause: Throwable? = null) : Exception("Не удалось связаться с ПК: он включён и в той же сети Wi-Fi?", cause)

    private fun certificate(pem: String): X509Certificate =
        CertificateFactory.getInstance("X.509").generateCertificate(pem.byteInputStream()) as X509Certificate

    fun fingerprint(pem: String): String =
        MessageDigest.getInstance("SHA-256").digest(certificate(pem).encoded).joinToString("") { "%02x".format(it) }

    /** Клиент, которому верят только сертификаты, выпущенные CA этого ПК. Имя хоста не проверяем: IP меняется. */
    fun pinnedClient(base: OkHttpClient, caPem: String): OkHttpClient {
        val store = KeyStore.getInstance(KeyStore.getDefaultType()).apply {
            load(null)
            setCertificateEntry("phonenect", certificate(caPem))
        }
        val trust = TrustManagerFactory.getInstance(TrustManagerFactory.getDefaultAlgorithm())
            .apply { init(store) }.trustManagers.first() as X509TrustManager
        val ssl = SSLContext.getInstance("TLS").apply { init(null, arrayOf(trust), null) }
        return base.newBuilder().sslSocketFactory(ssl.socketFactory, trust).hostnameVerifier { _, _ -> true }.build()
    }

    /** Первое знакомство: скачиваем CA без проверки и сверяем с отпечатком из ссылки. */
    fun fetchCa(base: OkHttpClient, url: String, fp: String): String {
        val trustAll = object : X509TrustManager {
            override fun checkClientTrusted(chain: Array<X509Certificate>, authType: String) {}
            override fun checkServerTrusted(chain: Array<X509Certificate>, authType: String) {}
            override fun getAcceptedIssuers(): Array<X509Certificate> = emptyArray()
        }
        val ssl = SSLContext.getInstance("TLS").apply { init(null, arrayOf(trustAll), null) }
        val client = base.newBuilder().sslSocketFactory(ssl.socketFactory, trustAll).hostnameVerifier { _, _ -> true }.build()
        val (pem, actual) = try {
            val pem = client.newCall(Request.Builder().url("$url/ca.crt").build()).execute().use { response ->
                if (!response.isSuccessful) throw IOException("ПК ответил ${response.code}")
                val source = response.body.source()
                // Сертификат — пара килобайт; больше 64 КБ — это не он.
                if (source.request(MAX_CA + 1)) throw IOException("слишком большой ответ")
                source.buffer.readUtf8()
            }
            pem to fingerprint(pem)
        } catch (e: IOException) {
            throw Unreachable(e)
        } catch (e: GeneralSecurityException) {
            throw Unreachable(e)
        } catch (e: ClassCastException) {
            throw Unreachable(e)
        }
        if (actual != fp.lowercase()) throw Mismatch()
        return pem
    }

    private const val MAX_CA = 64L * 1024
}
